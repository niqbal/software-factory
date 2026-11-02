"""Durable state: work items, jobs, append-only events, model runs, approvals.

SQLite in WAL mode is the single-node backend. The interface is small on purpose
so a Postgres implementation can replace it for the hosted multi-tenant mode.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from .models import WorkItem
from .util import iso, new_id, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
  id TEXT PRIMARY KEY, source_kind TEXT, external_id TEXT, payload TEXT, created REAL,
  UNIQUE(source_kind, external_id));
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, work_item_id TEXT, state TEXT, tier INTEGER, data TEXT,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, type TEXT, actor TEXT,
  payload TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, role TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
  cost_usd REAL, ts REAL);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, approver TEXT, decision TEXT,
  reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, seq);
CREATE INDEX IF NOT EXISTS runs_ts ON runs(ts);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # --- work items -------------------------------------------------------------
    def add_work_item(self, item: WorkItem) -> tuple[WorkItem, bool]:
        """Insert, deduplicating on (source, external id). Returns (item, created)."""
        with self._lock:
            row = self.db.execute(
                "SELECT payload FROM work_items WHERE source_kind=? AND external_id=?",
                (item.source_kind, item.external_id)).fetchone()
            if row:
                return WorkItem.from_dict(json.loads(row["payload"])), False
            item.id = item.id or new_id("WI")
            self.db.execute("INSERT INTO work_items VALUES (?,?,?,?,?)",
                            (item.id, item.source_kind, item.external_id,
                             json.dumps(item.to_dict()), now()))
            return item, True

    def get_work_item(self, item_id: str) -> WorkItem:
        row = self.db.execute("SELECT payload FROM work_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise KeyError(item_id)
        return WorkItem.from_dict(json.loads(row["payload"]))

    # --- jobs ---------------------------------------------------------------------
    def create_job(self, item: WorkItem, data: dict[str, Any] | None = None) -> str:
        with self._lock:
            existing = self.db.execute(
                "SELECT id FROM jobs WHERE work_item_id=? AND state NOT IN "
                "('failed','rejected','done','needs_human')", (item.id,)).fetchone()
            if existing:
                return existing["id"]
            job_id = new_id("JOB")
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                            (job_id, item.id, "queued", None, json.dumps(data or {}), now(), now()))
        self.event(job_id, "job.created", "factory", {"work_item": item.id, "title": item.title})
        return job_id

    def job(self, job_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

    def jobs(self, states: list[str] | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM jobs", []
        if states:
            q += f" WHERE state IN ({','.join('?' * len(states))})"
            args = states
        rows = self.db.execute(q + " ORDER BY created", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"] or "{}")
            out.append(d)
        return out

    def update_job(self, job_id: str, state: str | None = None, tier: int | None = None,
                   **data: Any) -> None:
        with self._lock:
            cur = self.job(job_id)
            merged = {**cur["data"], **data}
            new_state = state or cur["state"]
            self.db.execute("UPDATE jobs SET state=?, tier=?, data=?, updated=? WHERE id=?",
                            (new_state, tier if tier is not None else cur["tier"],
                             json.dumps(merged, default=str), now(), job_id))
        if state and state != cur["state"]:
            self.event(job_id, "job.state", "factory", {"from": cur["state"], "to": state})

    # --- events -------------------------------------------------------------------
    def event(self, job_id: str, type_: str, actor: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.db.execute("INSERT INTO events (job_id, type, actor, payload, ts) VALUES (?,?,?,?,?)",
                            (job_id, type_, actor, json.dumps(payload or {}, default=str), now()))

    def events(self, job_id: str | None = None) -> Iterator[dict[str, Any]]:
        q = "SELECT * FROM events" + (" WHERE job_id=?" if job_id else "") + " ORDER BY seq"
        for r in self.db.execute(q, (job_id,) if job_id else ()):
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            d["time"] = iso(d["ts"])
            yield d

    # --- model spend --------------------------------------------------------------
    def record_run(self, job_id: str, role: str, model: str, usage: dict[str, int], cost: float) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO runs (job_id, role, model, input_tokens, output_tokens, cache_read,"
                " cache_write, cost_usd, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, role, model, usage.get("input", 0), usage.get("output", 0),
                 usage.get("cache_read", 0), usage.get("cache_write", 0), cost, now()))

    def job_cost(self, job_id: str) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM runs WHERE job_id=?",
                              (job_id,)).fetchone()
        return float(row["c"])

    def spend_since(self, ts: float) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM runs WHERE ts>=?", (ts,)).fetchone()
        return float(row["c"])

    def runs(self, job_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM runs" + (" WHERE job_id=?" if job_id else "")
        return [dict(r) for r in self.db.execute(q, (job_id,) if job_id else ())]

    # --- approvals and flags ------------------------------------------------------
    def approve(self, job_id: str, approver: str, decision: str, reason: str = "") -> None:
        with self._lock:
            self.db.execute("INSERT INTO approvals (job_id, approver, decision, reason, ts) VALUES (?,?,?,?,?)",
                            (job_id, approver, decision, reason, now()))
        self.event(job_id, f"approval.{decision}", approver, {"reason": reason})

    def approvals(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM approvals WHERE job_id=?", (job_id,))]

    def set_flag(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self.db.execute("DELETE FROM flags WHERE key=?", (key,))
            else:
                self.db.execute("INSERT OR REPLACE INTO flags VALUES (?,?)", (key, value))

    def flag(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM flags WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
