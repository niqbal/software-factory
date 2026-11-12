"""Regression tests for merge-from-review UI behavior."""

import time
import urllib.error

from test_pipeline import factory, item, provider
from test_ui import KEY, get, post, start


def _wait_until(check, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for condition")


def test_merge_click_starts_once_updates_timeline_and_hides_merge(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider(), **{"observe.post_merge": ["true"]})
    job_id, _ = f.submit(item())
    assert f.run_job(job_id) == "awaiting_review"

    calls = {"n": 0}
    release = {"ok": False}
    original_merge = f.delivery.merge

    def blocking_merge(job, delivery):
        calls["n"] += 1
        while not release["ok"]:
            time.sleep(0.02)
        return original_merge(job, delivery)

    f.delivery.merge = blocking_merge
    console, server, base = start(f)
    try:
        msg = post(f"{base}/api/p/{KEY}/jobs/{job_id}/merge", console.token)["message"]
        assert "merging" in msg

        _wait_until(lambda: store.job(job_id)["state"] == "merging")
        detail = get(f"{base}/api/p/{KEY}/jobs/{job_id}")
        assert detail["state"] == "merging"
        assert "merge" not in detail["actions"]
        assert any(e["type"] == "job.state" and "awaiting_review" in e["summary"] and "merging" in e["summary"]
                   for e in detail["events"])

        try:
            post(f"{base}/api/p/{KEY}/jobs/{job_id}/merge", console.token)
            raise AssertionError("duplicate merge click was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        release["ok"] = True
        _wait_until(lambda: store.job(job_id)["state"] == "done")

        assert calls["n"] == 1
        assert sum(1 for e in store.events(job_id)
                   if e["type"] == "job.state" and e["payload"].get("to") == "merging") == 1
    finally:
        release["ok"] = True
        server.shutdown()


def test_merge_failure_surfaces_as_failed_state_and_timeline(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider(), **{"observe.post_merge": ["true"]})
    job_id, _ = f.submit(item())
    assert f.run_job(job_id) == "awaiting_review"

    calls = {"n": 0}

    def broken_merge(_job, _delivery):
        calls["n"] += 1
        raise RuntimeError("merge initiation failed")

    f.delivery.merge = broken_merge
    console, server, base = start(f)
    try:
        msg = post(f"{base}/api/p/{KEY}/jobs/{job_id}/merge", console.token)["message"]
        assert "merging" in msg

        _wait_until(lambda: store.job(job_id)["state"] == "failed")
        detail = get(f"{base}/api/p/{KEY}/jobs/{job_id}")

        assert detail["state"] == "failed"
        assert "merge" not in detail["actions"]
        assert "merge initiation failed" in (detail["data"].get("error") or "")
        assert any(e["type"] == "job.state" and "awaiting_review" in e["summary"] and "merging" in e["summary"]
                   for e in detail["events"])
        assert any(e["type"] == "job.state" and "merging" in e["summary"] and "failed" in e["summary"]
                   for e in detail["events"])
        assert calls["n"] == 1
    finally:
        server.shutdown()
