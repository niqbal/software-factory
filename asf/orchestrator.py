"""The factory: a resumable job state machine over the adapters.

Each state handler does one step and moves the job forward. State is written to
the store after every step, so a crashed or interrupted run resumes where it
stopped with `asf run --job <id>`.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import limits, policy
from .adapters import base as adapters
from .adapters.base import AdapterContext, ModelProvider, Sandbox
from .agents.roles import BudgetExceeded, PromptBuilder, run_agent, work_item_block
from .broker import ToolBroker
from .config import Config
from .gates import GateRunner
from .models import DELIVERED, TERMINAL, WAITING, AgentResult, GateResult, WorkItem
from .report import render_report
from .store import Store
from .util import any_match, git, run, sandbox_env, slugify

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "likely_paths": {"type": "array", "items": {"type": "string"}},
        "tier_guess": {"type": "integer", "enum": [0, 1, 2, 3]},
        "automatable": {"type": "boolean"},
        "reason_if_not": {"type": "string"},
        "clarification_needed": {"type": "string"},
        "manual_verification": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "acceptance_criteria", "likely_paths", "tier_guess", "automatable",
                 "reason_if_not", "clarification_needed", "manual_verification"],
    "additionalProperties": False,
}
MONTH = 30 * 24 * 3600
# Errors worth one more try without a human: the backend or network hiccuped, the work didn't.
TRANSIENT_ERRORS = [re.compile(p, re.I) for p in (
    r"\bHTTP 5\d\d\b", r"\[unavailable\]", r"service unavailable", r"bad gateway",
    r"gateway time-?out", r"timed? ?out", r"connection (reset|refused|aborted|error)",
    r"temporar(il)?y", r"network", r"ECONNRESET", r"EPIPE", r"broken pipe", r"internal server error",
)]


class Factory:
    def __init__(self, repo: Path, cfg: Config, state_dir: Path, store: Store,
                 provider: ModelProvider | None = None,
                 secrets: Callable[[str], str | None] | None = None,
                 echo: Callable[[str], None] | None = None):
        self.repo, self.cfg, self.state_dir, self.store = repo, cfg, state_dir, store
        self.echo = echo or (lambda msg: None)
        ctx_kwargs: dict[str, Any] = {"repo": repo, "state_dir": state_dir, "config": cfg, "store": store}
        if secrets:
            ctx_kwargs["secrets"] = secrets
        self.ctx = AdapterContext(**ctx_kwargs)
        ex = cfg.get("execution")
        self.sandboxes = adapters.load("sandbox", ex["provider"])(ex, self.ctx)
        dl = cfg.get("delivery")
        self.delivery = adapters.load("delivery", dl["provider"])(dl, self.ctx)
        ob = cfg.get("observe")
        self.observe = adapters.load("observe", ob.get("provider", "none"))(ob, self.ctx)
        self.notifiers = [adapters.load("notify", n)(cfg.get(f"notifications.{n}", {}) or {}, self.ctx)
                          for n in cfg.get("notifications.providers", ["console"])]
        self._provider = provider
        self._prompts: PromptBuilder | None = None

    # ------------------------------------------------------------- plumbing --
    @property
    def provider(self) -> ModelProvider:
        if self._provider is None:
            m = self.cfg.get("models")
            self._provider = adapters.load("models", m.get("provider", "anthropic"))(m, self.ctx)
        return self._provider

    def intake_adapters(self) -> dict[str, adapters.IntakeAdapter]:
        out = {}
        for kind, settings in (self.cfg.get("intake") or {}).items():
            settings = settings or {}
            if settings.get("enabled", True):
                out[kind] = adapters.load("intake", kind)(settings, self.ctx)
        return out

    def paused(self) -> str | None:
        if self.store.flag("paused_all"):
            return f"kill switch: {self.store.flag('paused_all')}"
        if (self.state_dir / "PAUSED").exists():
            return "kill switch file present"
        return None

    def notify(self, event: str, job: dict[str, Any], message: str) -> None:
        self.store.event(job["id"], f"notify.{event}", "factory", {"message": message})
        if event in self.cfg.get("notifications.on", []):
            for n in self.notifiers:
                try:
                    n.notify(event, job, message)
                except Exception as e:  # noqa: BLE001 - notifications never break a job
                    self.store.event(job["id"], "notify.error", n.kind, {"error": str(e)})

    def _log(self, job_id: str) -> Callable[[str, dict], None]:
        return lambda type_, payload: self.store.event(job_id, type_, "factory", payload)

    # --------------------------------------------------------------- intake --
    def submit(self, item: WorkItem, rerun: bool = False) -> tuple[str, bool]:
        """Return (job_id, new_job). An item that already has a job keeps it (see Store.create_job)."""
        item, _ = self.store.add_work_item(item)
        return self.store.create_job(item, {"started": time.time()}, rerun=rerun)

    def poll(self, source: str | None = None, ids: list[str] | None = None,
             rerun: bool = False) -> list[str]:
        """Pull items and return the ids of jobs created by this call."""
        jobs = []
        for kind, adapter in self.intake_adapters().items():
            if source and kind != source:
                continue
            items = [adapter.fetch(i) for i in ids] if ids else adapter.poll()
            for item in items:
                job_id, new_job = self.submit(item, rerun=rerun)
                if new_job:
                    self.echo(f"queued {job_id} <- {kind}:{item.external_id} {item.title}")
                    jobs.append(job_id)
                else:
                    state = self.store.job(job_id)["state"]
                    self.echo(f"skip   {kind}:{item.external_id} already has {job_id} ({state})")
        return jobs

    # ----------------------------------------------------------------- run ---
    MODEL_STATES = {"queued", "implementing", "testing", "verifying", "reviewing"}

    def require_model(self) -> None:
        """Refuse to start model work without credentials (ValueError, nothing changed)."""
        problem = self.provider.credentials_problem()
        if problem:
            raise ValueError(f"model credentials missing: {problem}")

    def run_job(self, job_id: str, stop_at: str | None = None) -> str:
        if self.store.job(job_id)["state"] in self.MODEL_STATES and \
                self.store.job(job_id)["state"] != stop_at:
            self.require_model()
        while True:
            job = self.store.job(job_id)
            state = job["state"]
            if state in TERMINAL or state == "paused" or state == stop_at:
                return state
            if state in WAITING and not self._waiting_settled(job):
                return state
            reason = self.paused()
            if reason:
                self.store.update_job(job_id, "paused", resume_state=state, paused_reason=reason)
                self.notify("paused", job, reason)
                return "paused"
            self.echo(f"[{job_id}] {state}")
            try:
                getattr(self, f"_s_{state}")(job)
            except BudgetExceeded as e:
                self.store.update_job(job_id, "paused", resume_state=state, paused_reason=str(e))
                self.notify("paused", job, f"budget: {e}")
                return "paused"
            except Exception as e:  # noqa: BLE001 - any failure parks the job for a human
                self.store.event(job_id, "job.error", "factory",
                                 {"state": state, "error": str(e), "trace": traceback.format_exc()[-4000:]})
                self.store.update_job(job_id, "failed", error=f"{state}: {e}", failed_state=state)
                self.notify("failed", self.store.job(job_id), f"{state}: {e}")
                return "failed"

    def resume(self, job_id: str, extra_usd: float = 0.0, extra_minutes: float = 0.0) -> None:
        job = self.store.job(job_id)
        if job["state"] != "paused":
            raise ValueError(f"{job_id} is {job['state']}, not paused")
        self.store.update_job(job_id, job["data"].get("resume_state", "queued"),
                              extra_budget_usd=job["data"].get("extra_budget_usd", 0) + extra_usd,
                              extra_minutes=job["data"].get("extra_minutes", 0) + extra_minutes)

    def retry(self, job_id: str, auto: bool = False, keep_work: bool = False) -> str:
        """Re-enter a failed or needs_human job at the step that stopped it.

        A human retry also resets the count of automatic retries (see auto_retry). With
        `keep_work`, an implementation interrupted by a transient error is continued from the
        half-finished working tree instead of starting again from the base commit."""
        job = self.store.job(job_id)
        if job["state"] not in ("failed", "needs_human"):
            raise ValueError(f"{job_id} is {job['state']}; only failed or needs_human jobs can be retried")
        target = job["data"].get("failed_state") or ("implementing" if job["data"].get("sandbox") else "queued")
        if target == "deploying":
            self.__dict__.pop("_deploy_cache", None)  # run the deploy again, not its cached failure
        if target == "merged":
            self.__dict__.pop("_post_merge_cache", None)  # likewise the post-merge checks
        partial: list[str] = []
        if target == "implementing" and job["data"].get("sandbox"):
            sb = self._sandbox(job)
            revision = self._active_revision(job)
            if keep_work:
                partial = sb.uncommitted_files() if revision else sb.changed_files()
            if partial:
                self.store.event(job_id, "sandbox.kept", "factory", {"reason": "retry", "files": partial[:200]})
            else:
                # A fresh attempt starts from the base commit, not from a half-finished one. During a
                # revision the base is the delivered commit: the work already on the branch is kept.
                sb.reset(revision["from_commit"] if revision else None)
                self.store.event(job_id, "sandbox.reset", "factory", {"reason": "retry"})
        self.store.update_job(job_id, target, error="", needs_human_reason="", feedback="",
                              tester_rounds=0, review_rounds=0, ci_rounds=0, failed_state="",
                              incomplete=False, partial_work=partial,
                              **({} if auto else {"auto_retries": 0, "auto_retry_exhausted": False}))
        return target

    def retry_reason(self, job: dict[str, Any]) -> str | None:
        """Why a stopped job can be retried without a human, or None when it needs one.

        Two kinds of stop qualify: a failure from a transient backend or network error, and a
        needs_human stop marked `incomplete` (the agent did not finish; nobody was asked anything).
        Usage limits are not handled here: the scheduler waits for the reset instead.
        """
        d = job["data"]
        if job["state"] == "failed":
            err = d.get("error") or ""
            if not limits.is_limit_error(err) and any(p.search(err) for p in TRANSIENT_ERRORS):
                return f"transient error: {err.strip()[:200]}"
        if job["state"] == "needs_human" and d.get("incomplete"):
            return f"incomplete: {(d.get('needs_human_reason') or '').strip()[:200]}"
        return None

    def auto_retry(self, job_id: str) -> str | None:
        """Retry a job that stopped without needing a human, up to `limits.auto_retries` times.

        Returns the state it re-enters, or None (nothing to retry, or the retries are used up;
        then the job stays parked for a human and an event says so)."""
        job = self.store.job(job_id)
        reason = self.retry_reason(job)
        if not reason:
            return None
        cap = int(self.cfg.get("limits.auto_retries", 2) or 0)
        done = int(job["data"].get("auto_retries", 0))
        if done >= cap:
            if not job["data"].get("auto_retry_exhausted"):
                self.store.update_job(job_id, auto_retry_exhausted=True)
                self.store.event(job_id, "job.auto_retry_exhausted", "factory",
                                 {"retries": done, "reason": reason})
                self.notify("needs_human", self.store.job(job_id),
                            f"gave up after {done} automatic retr{'y' if done == 1 else 'ies'}; {reason}")
            return None
        self.store.event(job_id, "job.auto_retry", "factory",
                         {"attempt": done + 1, "of": cap, "from": job["state"], "reason": reason})
        self.store.update_job(job_id, auto_retries=done + 1)
        # The backend failed, not the work: continue it. An incomplete attempt starts afresh.
        return self.retry(job_id, auto=True, keep_work=job["state"] == "failed")

    def rerun(self, job_id: str, by: str = "human") -> str:
        """Replace a stopped job with a new job for the same work item, from the current default branch.

        The item is fetched again from its own intake source (it may have been edited). Nothing
        changes unless the new job is created: a failed fetch leaves the old job as it was and
        records why on it. Returns the new job's id."""
        job = self.store.job(job_id)
        if job["state"] not in ("rejected", "failed", "needs_human"):
            raise ValueError(f"{job_id} is {job['state']}; only failed, needs_human or rejected jobs can be rerun")
        stored = self.store.get_work_item(job["work_item_id"])
        try:
            adapter = self.intake_adapters().get(stored.source_kind)
            if adapter is None:
                raise ValueError(f"intake {stored.source_kind!r} is not enabled in factory.yaml")
            fresh = adapter.fetch(stored.external_id)
        except Exception as e:  # noqa: BLE001 - recorded on the job, then refused
            msg = f"could not fetch {stored.source_kind}:{stored.external_id}: {e}"
            self.store.event(job_id, "job.rerun_failed", "factory", {"by": by, "error": msg})
            raise ValueError(msg) from e
        fresh.id = stored.id
        self.store.update_work_item(fresh)
        new_id, created = self.store.create_job(fresh, {"started": time.time(), "rerun_of": job_id}, rerun=True)
        if not created:
            msg = f"{stored.source_kind}:{stored.external_id} already has an active job {new_id}"
            self.store.event(job_id, "job.rerun_failed", "factory", {"by": by, "error": msg})
            raise ValueError(msg)
        if job["state"] != "rejected":
            self.reject_job(job_id, by=by, reason=f"replaced by rerun {new_id}")
        self.store.update_job(job_id, rerun_as=new_id)
        self.store.event(job_id, "job.rerun", "human", {"by": by, "new_job": new_id})
        self.echo(f"rerun {job_id} -> {new_id}")
        return new_id

    def revise(self, job_id: str, notes: list[str], extra_usd: float = 0.0) -> str:
        """Send a delivered, unmerged job back to the implementer with a reviewer's notes.

        The job keeps its branch: the implementer works on top of the delivered commit, then
        the tester, gates and reviewers run again over the whole change, and a new commit is
        added to the same branch. Each revision gets a fresh budget.
        """
        notes = [n.strip() for n in notes if n and n.strip()]
        if not notes:
            raise ValueError("give at least one revision note")
        job = self.store.job(job_id)
        d = job["data"]
        tip = self._delivered_branch_tip(job, "revised")
        sandbox_root = (d.get("sandbox") or {}).get("root")
        branch = d["branch"]
        checked_out = [line.split(" ", 1)[1] for line in
                       git(self.repo, "worktree", "list", "--porcelain").splitlines()
                       if line.startswith("worktree ")]
        for wt in checked_out:
            if wt != sandbox_root and git(Path(wt), "rev-parse", "--abbrev-ref", "HEAD",
                                          check=False).strip() == branch:
                raise ValueError(f"{branch} is checked out in {wt}; switch that checkout to another "
                                 "branch first so the factory can work on it")
        revisions = list(d.get("revisions", []))
        revisions.append({"n": len(revisions) + 1, "notes": notes, "from_commit": tip,
                          "requested_at": time.time(), "cost_before": round(self.store.job_cost(job_id), 4)})
        # Fresh budget for this revision: the tier's cap applies to new spend only.
        extra = self.store.job_cost(job_id) + extra_usd
        self.store.update_job(job_id, "implementing", revisions=revisions, started=time.time(),
                              extra_budget_usd=round(extra, 4),
                              feedback="", error="", needs_human_reason="", outcome="",
                              tester_rounds=0, review_rounds=0, ci_rounds=0)
        self.store.event(job_id, "job.revise", "human", {"revision": len(revisions), "notes": notes,
                                                         "from_commit": tip})
        return "implementing"

    def _delivered_branch_tip(self, job: dict[str, Any], verb: str) -> str:
        """Tip of a delivered, unmerged job's branch, or ValueError saying why it can't be used."""
        d = job["data"]
        merged = bool(d.get("merged")) or d.get("outcome") in ("merged", "shipped")
        if job["state"] != "awaiting_review" or merged:
            raise ValueError(f"{job['id']} is {job['state']}{' and merged' if merged else ''}; "
                             f"only delivered, unmerged jobs can be {verb}")
        branch = d.get("branch")
        if not branch or not git(self.repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}",
                                 check=False).strip():
            raise ValueError(f"{job['id']}: branch {branch or '(none)'} no longer exists")
        tip = git(self.repo, "rev-parse", f"refs/heads/{branch}").strip()
        if run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", tip, self.cfg.default_branch],
               shell=False).ok:
            raise ValueError(f"{branch} is already merged into {self.cfg.default_branch}; "
                             "open a new work item for further changes")
        return tip

    @property
    def github(self) -> Any:
        """The GitHub delivery adapter, whatever delivery the config uses for new jobs."""
        if self.delivery.kind == "github":
            return self.delivery
        if not hasattr(self, "_github"):
            self._github = adapters.load("delivery", "github")(self.cfg.get("delivery.github") or {}, self.ctx)
        return self._github

    def _delivery_for(self, job: dict[str, Any]) -> Any:
        """A job published as a pull request keeps delivering there (revisions update the PR)."""
        return self.github if job["data"].get("pr") else self.delivery

    def publish(self, job_id: str) -> dict[str, Any]:
        """Push a delivered job's branch and open (or update) a draft pull request for it."""
        job = self.store.job(job_id)
        self._delivered_branch_tip(job, "published")
        d = job["data"]
        item = self._item(job)
        report_path = Path(d["report"]) if d.get("report") else None
        report = report_path.read_text() if report_path and report_path.exists() else \
            render_report(job, item, self.store.runs(job_id), self.store.job_cost(job_id))
        sb = type("BranchRef", (), {"branch": d["branch"], "base_sha": (d.get("sandbox") or {}).get("base_sha", "")})
        result = self.github.deliver(job, item, sb, report, draft=True)
        pr = {**(d.get("pr") or {}), "url": result["url"], "number": result["number"]}
        pr.setdefault("seen", [])
        self.store.update_job(job_id, pr=pr)
        self.store.event(job_id, "job.published", "factory", {"url": result["url"], "updated": result["updated"]})
        return result

    def reviews(self, job_id: str | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        """Act on `/revise` comments on factory pull requests.

        A trusted comment (repo owner, member or collaborator) whose first line starts with
        `/revise` sends the job back with notes: the rest of that comment, plus every trusted
        line comment on the diff not yet used by an earlier revision. Untrusted comments are
        ignored and logged. The result is posted back to the pull request.
        """
        gh = self.github
        actions = []
        jobs = [self.store.job(job_id)] if job_id else [j for j in self.store.jobs() if j["data"].get("pr")]
        for job in jobs:
            pr = job["data"].get("pr")
            if not pr or not pr.get("number"):
                continue
            number = pr["number"]
            state = gh.pr_state(number)
            if state != "OPEN":
                actions.append({"job": job["id"], "pr": number, "action": f"skip: PR is {state.lower()}"})
                continue
            seen = set(pr.get("seen", []))
            fresh = [c for c in gh.comments(number) if c["id"] not in seen]
            ignored = [c for c in fresh if not gh.trusted(c)]
            trusted = [c for c in fresh if gh.trusted(c)]
            triggers = [c for c in trusted if c["kind"] in ("comment", "review")
                        and c["body"].splitlines()[0].strip().lower().startswith("/revise")]
            lines = [c for c in trusted if c["kind"] == "line"]
            chatter = [c for c in trusted if c["kind"] != "line" and c not in triggers]
            if not dry_run:
                for c in ignored:
                    self.store.event(job["id"], "pr.comment_ignored", "factory",
                                     {"id": c["id"], "author": c["author"], "association": c["association"]})
            if not triggers:
                # Discussion and untrusted comments are done with; line comments wait for a /revise.
                if not dry_run and (ignored or chatter):
                    self._mark_seen(job["id"], [c["id"] for c in ignored + chatter])
                actions.append({"job": job["id"], "pr": number,
                                "action": f"no /revise yet ({len(lines)} line comment(s) waiting)"})
                continue
            notes = []
            for t in triggers:
                rest = t["body"].strip()[len("/revise"):].strip()
                if rest:
                    notes.append(rest)
            notes += [f"{c['path']}:{c['line']}: {c['body']}" if c.get("path") else c["body"] for c in lines]
            act = {"job": job["id"], "pr": number, "action": "revise", "notes": notes}
            actions.append(act)
            if dry_run:
                continue
            if job["state"] == "awaiting_review" and notes:
                # Before the comments are marked as handled: a missing key must leave them pending.
                self.require_model()
            self._mark_seen(job["id"], [c["id"] for c in fresh])
            if job["state"] != "awaiting_review":
                gh.comment(number, f"The factory is still working on `{job['id']}` ({job['state']}); "
                                   "comment `/revise` again once it has finished.")
                act["action"] = f"skip: job is {job['state']}"
                continue
            if not notes:
                gh.comment(number, "`/revise` needs notes: write them after the command, or leave line "
                                   "comments on the diff first.")
                act["action"] = "skip: no notes"
                continue
            try:
                self.revise(job["id"], notes)
            except ValueError as e:
                gh.comment(number, f"Could not revise `{job['id']}`: {e}")
                act["action"] = f"refused: {e}"
                continue
            self.echo(f"revising {job['id']} from PR #{number} with {len(notes)} note(s)")
            final = self.run_job(job["id"])
            done = self.store.job(job["id"])
            act["result"] = final
            rev = (done["data"].get("revisions") or [{}])[-1]
            if final in ("awaiting_review", "done") and rev.get("commit"):
                msg = (f"**Revision {rev['n']}** pushed as {rev['commit'][:10]}. Notes addressed:\n"
                       + "\n".join(f"- {n}" for n in notes)
                       + f"\n\n{(done['data'].get('implementer') or '').strip()[:1500]}")
            else:
                reason = done["data"].get("needs_human_reason") or done["data"].get("error") or final
                msg = f"Revision stopped at `{final}`: {str(reason)[:1500]}"
            gh.comment(number, msg + f"\n\n<sub>Factory job `{job['id']}`</sub>")
        return actions

    def _mark_seen(self, job_id: str, ids: list[str]) -> None:
        pr = dict(self.store.job(job_id)["data"].get("pr") or {})
        pr["seen"] = sorted(set(pr.get("seen", [])) | set(ids))
        self.store.update_job(job_id, pr=pr)

    def _active_revision(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """The revision in progress, if the job was sent back with `revise` and is not done yet."""
        revisions = job["data"].get("revisions") or []
        return revisions[-1] if revisions and job["state"] not in DELIVERED | {"done"} \
            and not revisions[-1].get("commit") \
            else None

    # -------------------------------------------------------------- budgets --
    def _charger(self, job_id: str) -> Callable[[str, Any], None]:
        def charge(role: str, turn: Any) -> None:
            self.store.record_run(job_id, role, turn.model, turn.usage, turn.cost_usd)
            self._check_budget(job_id)
        return charge

    def _check_budget(self, job_id: str) -> None:
        job = self.store.job(job_id)
        tier = job["tier"] if job["tier"] is not None else job["data"].get("pre_tier", 2)
        budget = self.cfg.budget_for(tier)
        cap = budget.get("usd", 5.0) + job["data"].get("extra_budget_usd", 0)
        spent = self.store.job_cost(job_id)
        if spent > cap:
            raise BudgetExceeded(f"job spent ${spent:.2f} of ${cap:.2f} (tier {tier})")
        minutes = (time.time() - job["data"].get("started", time.time())) / 60
        limit = budget.get("minutes", 90) + job["data"].get("extra_minutes", 0)
        if minutes > limit:
            raise BudgetExceeded(f"job ran {minutes:.0f} of {limit} minutes")
        monthly = self.store.spend_since(time.time() - MONTH)
        month_cap = self.cfg.get("budgets.monthly_cap_usd", 50)
        if monthly > month_cap:
            raise BudgetExceeded(f"monthly spend ${monthly:.2f} exceeds cap ${month_cap:.2f}")

    # ------------------------------------------------------------- helpers ---
    def _item(self, job: dict[str, Any]) -> WorkItem:
        return self.store.get_work_item(job["work_item_id"])

    def _sandbox(self, job: dict[str, Any]) -> Sandbox:
        return self.sandboxes.reopen(job["data"]["sandbox"])

    def prompts(self, sb: Sandbox) -> PromptBuilder:
        if self._prompts is None:
            self._prompts = PromptBuilder(self.cfg, sb.root)
        return self._prompts

    def _agent(self, job: dict[str, Any], role: str, task: str, tier: int) -> AgentResult:
        self._check_budget(job["id"])
        sb = self._sandbox(job)
        broker = ToolBroker(sb, self.cfg, role, tier, self.store, job["id"])
        system = self.prompts(sb).system(role, tier)
        self.store.event(job["id"], "agent.start", role, {"tier": tier,
                                                          "model": self.cfg.role_model(role).get("model")})
        result = run_agent(self.provider, self.cfg, role, system, task, broker, self._charger(job["id"]))
        self.store.event(job["id"], "agent.end", role, {
            "ok": result.ok, "turns": result.turns, "cost_usd": round(result.cost_usd, 4),
            "summary": result.summary[:2000], "data": {k: v for k, v in result.data.items() if k != "summary"}})
        self.echo(f"  {role}: {'ok' if result.ok else 'not ok'} in {result.turns} turns, ${result.cost_usd:.2f}")
        return result

    def _current_tier(self, job: dict[str, Any]) -> int:
        return job["tier"] if job["tier"] is not None else job["data"].get("pre_tier", 2)

    def _needs_human(self, job: dict[str, Any], reason: str, incomplete: bool = False) -> None:
        """Park a job for a human. `incomplete` marks a stop where the work simply did not
        finish and no human decision is asked for, so the scheduler may retry it on its own."""
        self.store.update_job(job["id"], "needs_human", needs_human_reason=reason, incomplete=incomplete)
        self.notify("needs_human", self.store.job(job["id"]), reason)

    # -------------------------------------------------------------- states ---
    def _s_queued(self, job: dict[str, Any]) -> None:
        item = self._item(job)
        spec = self.cfg.role_model("triage")
        system = PromptBuilder(self.cfg, self.repo).system("triage", 1)
        if hasattr(self.provider, "structured_for"):
            triage, turn = self.provider.structured_for("triage", system, work_item_block(item, item.acceptance),
                                                        TRIAGE_SCHEMA)
        else:
            triage, turn = self.provider.structured(spec["model"], system, work_item_block(item, item.acceptance),
                                                    TRIAGE_SCHEMA, spec.get("effort", "low"))
        self._charger(job["id"])("triage", turn)
        pre = policy.pre_tier(self.cfg, item, triage.get("tier_guess"))
        acceptance = item.acceptance or triage.get("acceptance_criteria", [])
        self.store.event(job["id"], "triage", "triage", triage)
        self.store.update_job(job["id"], "triaged", tier=None, pre_tier=pre, triage=triage,
                              acceptance=acceptance, manual_verification=triage.get("manual_verification", []))
        if not triage.get("automatable", True):
            self._needs_human(self.store.job(job["id"]), f"not automatable: {triage.get('reason_if_not')}")
        elif _real_question(triage.get("clarification_needed")):
            self._needs_human(self.store.job(job["id"]), f"clarification needed: {triage['clarification_needed']}")

    def _s_triaged(self, job: dict[str, Any]) -> None:
        item = self._item(job)
        prefix = self.cfg.get("delivery.branch_prefix", "factory/")
        branch = f"{prefix}{slugify(item.title, 32)}-{job['id'].split('-')[-1]}"
        base_ref = job["data"].get("base_ref") or self.cfg.default_branch
        sb = self.sandboxes.create(job["id"], base_ref, branch)
        self.store.update_job(job["id"], "implementing", sandbox=sb.describe(), branch=branch)

    def _task(self, job: dict[str, Any], extra: str = "") -> str:
        item = self._item(job)
        text = work_item_block(item, job["data"].get("acceptance", []))
        revision = self._active_revision(job)
        if revision:
            text += ("\n\nThis change was already delivered on this branch; the working tree contains it. "
                     "A human reviewed it and asked for revisions. Keep what is right, change only what "
                     "the notes ask for, and do not start over:\n"
                     + "\n".join(f"- {n}" for n in revision["notes"]))
        partial = job["data"].get("partial_work") or []
        if partial:
            listed = "\n".join(f"- {p}" for p in partial[:50]) + (
                f"\n- ... and {len(partial) - 50} more" if len(partial) > 50 else "")
            text += ("\n\nAn earlier attempt at this task was cut off by a backend error, not by a problem "
                     "with the work. Its unfinished changes are still in the working tree, in these files:\n"
                     + listed + "\nRead them first. Keep what is right, fix what is not, and finish the "
                     "task from there; do not start over.")
        feedback = job["data"].get("feedback")
        if feedback:
            text += "\n\nThe previous attempt needs changes. Address these findings:\n" + feedback
        return text + (("\n\n" + extra) if extra else "")

    def _s_implementing(self, job: dict[str, Any]) -> None:
        tier = self._current_tier(job)
        res = self._agent(job, "implementer", self._task(job), tier)
        if job["data"].get("partial_work"):
            self.store.update_job(job["id"], partial_work=[])  # handed over; later rounds don't repeat it
        if res.data.get("blocked_reason"):
            # Blocked on its own initiative it needs a human. Blocked only after the factory
            # nudged it to finish, it was cut short: a fresh attempt may get through.
            return self._needs_human(job, f"implementer blocked: {res.data['blocked_reason']}",
                                     incomplete=bool(res.data.get("nudged")))
        sb = self._sandbox(job)
        changed = sb.uncommitted_files() if self._active_revision(job) else sb.changed_files()
        if not changed:
            # Nothing was produced and nobody was asked anything: the attempt just did not finish.
            return self._needs_human(job, f"implementer made no changes: {res.summary[:500]}",
                                     incomplete=True)
        self.store.update_job(job["id"], "testing", implementer=res.summary, feedback="",
                              acceptance_met=res.data.get("acceptance_met", []))

    def _s_testing(self, job: dict[str, Any]) -> None:
        tier = self._current_tier(job)
        item = self._item(job)
        task = (work_item_block(item, job["data"].get("acceptance", []))
                + "\n\nThe implementation is in the working tree. Read it with show_diff.")
        revision = self._active_revision(job)
        if revision:
            task += ("\n\nThis is a revision of a delivered change. Also check these review notes "
                     "were addressed:\n" + "\n".join(f"- {n}" for n in revision["notes"]))
        res = self._agent(job, "tester", task, tier)
        rounds = job["data"].get("tester_rounds", 0)
        defect = res.data.get("defect_found")
        if defect and rounds < self.cfg.get("limits.fix_rounds", 1):
            self.store.update_job(job["id"], "implementing", tester_rounds=rounds + 1,
                                  feedback=f"- Independent tester found a defect: {defect}")
            return
        self.store.update_job(job["id"], "verifying", tester=res.summary,
                              tests_added=res.data.get("tests_added", []), tester_defect=defect or "")

    def _s_verifying(self, job: dict[str, Any]) -> None:
        sb = self._sandbox(job)
        item = self._item(job)
        changed = sb.changed_files()
        tier = policy.final_tier(self.cfg, item, job["data"].get("pre_tier", 2), changed)
        decision = policy.decide(self.cfg, tier, manual_checks=job["data"].get("manual_verification", []))
        runner = GateRunner(self.cfg, sb, tier, self.state_dir,
                            base_sandbox=lambda: self.sandboxes.base_checkout(sb.base_sha),
                            log=self._log(job["id"]))
        results = runner.run(decision.deterministic)
        failed = [r for r in results if r.blocking]
        gate_rows = [r.__dict__ for r in results]
        self.store.update_job(job["id"], tier=tier, decision=decision.as_dict(), gates=gate_rows,
                              changed=changed)
        if not failed:
            self.store.update_job(job["id"], "reviewing")
            return
        rounds = job["data"].get("ci_rounds", 0)
        fixable = [r for r in failed if r.name in ("lint", "typecheck", "build", "test")]
        if fixable and rounds < self.cfg.get("limits.ci_fix_rounds", 1):
            logs = "\n\n".join(f"Gate {r.name} ({r.package}) failed: {r.detail}\n{r.log}" for r in fixable)
            res = self._agent(self.store.job(job["id"]), "ci_fixer", self._task(job, logs), tier)
            self.store.update_job(job["id"], ci_rounds=rounds + 1, ci_fixer=res.summary)
            if res.data.get("blocked_reason"):
                return self._needs_human(job, f"gates failed; CI fixer: {res.data['blocked_reason']}")
            return  # state stays verifying; gates re-run
        summary = "; ".join(f"{r.name}{'/' + r.package if r.package else ''}: {r.detail}" for r in failed)
        # Retry re-enters at verification with the same sandbox: the work stays, the gates rerun
        # (often after a human changed policy, e.g. a diff limit), and no model spend is repeated.
        self.store.update_job(job["id"], failed_state="verifying")
        self._needs_human(self.store.job(job["id"]), f"gates failed: {summary}")

    def _s_reviewing(self, job: dict[str, Any]) -> None:
        tier = self._current_tier(job)
        decision = job["data"]["decision"]
        item = self._item(job)
        gates = "\n".join(f"- {g['name']}{'/' + g['package'] if g['package'] else ''}: {g['status']} "
                          f"({g['detail']})" for g in job["data"].get("gates", []))
        task = (work_item_block(item, job["data"].get("acceptance", []))
                + f"\n\nRisk tier: {tier}. Deterministic gates already run:\n{gates}\n\n"
                  "Review the change in the working tree with show_diff.")
        reviews, blocking = {}, []
        for role in decision["llm"]:
            res = self._agent(self.store.job(job["id"]), role, task, tier)
            if not res.ok:
                return self._needs_human(job, f"{role} did not finish its review")
            reviews[role] = res.data
            for f in res.data.get("findings", []):
                if f.get("severity") == "blocking":
                    blocking.append(f"- [{role}] {f.get('file')}:{f.get('line', '')} {f.get('issue')}")
            if res.data.get("verdict") == "request_changes" and not blocking:
                blocking.append(f"- [{role}] requested changes: {res.data.get('summary')}")
        self.store.update_job(job["id"], reviews=reviews)
        if not blocking:
            self.store.update_job(job["id"], "committing")
            return
        rounds = job["data"].get("review_rounds", 0)
        if rounds < self.cfg.get("limits.fix_rounds", 1):
            self.store.update_job(job["id"], "implementing", review_rounds=rounds + 1,
                                  feedback="\n".join(blocking))
            return
        self._needs_human(job, "reviewers still block after fix rounds:\n" + "\n".join(blocking))

    def _s_committing(self, job: dict[str, Any]) -> None:
        sb = self._sandbox(job)
        item = self._item(job)
        revision = self._active_revision(job)
        title = f"Revise: {item.title}" if revision else f"{item.title}"
        body = ("\n\nReview notes addressed:\n" + "\n".join(f"- {n}" for n in revision["notes"])) \
            if revision else ""
        trailer = (f"\n\nFactory-Job: {job['id']}\nWork-Item: {item.source_kind}:{item.external_id}"
                   f"\nRisk-Tier: {job['tier']}" + (f"\nRevision: {revision['n']}" if revision else ""))
        sha = sb.commit(f"{title}{body}{trailer}")
        if revision:
            revisions = job["data"]["revisions"]
            revisions[-1] = {**revision, "commit": sha, "summary": job["data"].get("implementer", "")}
            self.store.update_job(job["id"], revisions=revisions)
        self.store.update_job(job["id"], owner_review=self._owner_review(job, sb, sha))
        job = self.store.job(job["id"])
        report = render_report(job, item, self.store.runs(job["id"]), self.store.job_cost(job["id"]))
        path = self.state_dir / "reports" / f"{job['id']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report)
        self.store.update_job(job["id"], "delivering", commit=sha, report=str(path))

    def owner_paths(self) -> list[str]:
        """Paths only the owner may approve: owner_review.paths, else every tier-3 path rule."""
        explicit = self.cfg.get("owner_review.paths")
        if explicit:
            return list(explicit)
        return [p for r in self.cfg.get("tiers.rules", []) if r.get("tier", 0) >= 3 for p in r["paths"]]

    def _owner_review(self, job: dict[str, Any], sb: Sandbox, sha: str) -> dict[str, Any]:
        """The small part of a change the owner must look at: owner-only files, their diff, and
        the agent's OWNER REVIEW notes (AGENTS.md convention)."""
        files = sorted(set(git(Path(sb.root), "diff", "--name-only", sb.base_sha, sha).split()))
        owned = [f for f in files if any_match(f, self.owner_paths())]
        notes = owner_notes(job["data"].get("implementer", ""))
        if not owned and not notes:
            return {}
        diff = git(Path(sb.root), "diff", sb.base_sha, sha, "--", *owned) if owned else ""
        return {"files": owned, "notes": notes, "diff": diff[:20000], "truncated": len(diff) > 20000}

    def _s_delivering(self, job: dict[str, Any]) -> None:
        d = job["data"]["decision"]
        item = self._item(job)
        report = Path(job["data"]["report"]).read_text()
        draft = bool(self.cfg.get("delivery.draft", True)) or not d["may_agent_merge"]
        result = self._delivery_for(job).deliver(job, item, self._sandbox(job), report, draft=draft)
        self.store.update_job(job["id"], delivery=result)
        if result.get("kind") == "github" and result.get("number"):
            pr = {**(job["data"].get("pr") or {}), "url": result["url"], "number": result["number"]}
            pr.setdefault("seen", [])
            self.store.update_job(job["id"], pr=pr)
        self.store.update_job(job["id"], delivered_at=time.time())
        if not d["may_agent_merge"]:
            # Delivered is not done: the job waits for a human to merge, then verifies the merge.
            self.store.update_job(job["id"], "awaiting_review", outcome="")
            self._release_sandbox(job)  # frees the branch so the reviewer can check it out
            self.notify("awaiting_review", self.store.job(job["id"]),
                        f"{result.get('url') or result.get('branch')} is ready for review ({d['reason']})")
            return
        next_state = "awaiting_approval" if d["approvals_required"] > 0 else "merging"
        self.store.update_job(job["id"], next_state)
        if next_state == "awaiting_approval":
            self.notify("awaiting_approval", self.store.job(job["id"]),
                        f"{result.get('url') or result.get('branch')} needs {d['approvals_required']} approval(s)")

    def _approvals_settled(self, job: dict[str, Any]) -> bool:
        approvals = self.store.approvals(job["id"])
        if any(a["decision"] == "reject" for a in approvals):
            self.store.update_job(job["id"], "rejected")
            return True
        need = job["data"]["decision"]["approvals_required"]
        if sum(1 for a in approvals if a["decision"] == "approve") >= need:
            self.store.update_job(job["id"], "merging")
            return True
        return False

    def _s_awaiting_approval(self, job: dict[str, Any]) -> None:
        return None  # handled by _approvals_settled

    def _s_awaiting_review(self, job: dict[str, Any]) -> None:
        return None  # handled by _review_settled

    def _waiting_settled(self, job: dict[str, Any]) -> bool:
        """True when a waiting job's condition resolved (its state has changed)."""
        return {"awaiting_approval": self._approvals_settled,
                "awaiting_review": self._review_settled,
                "awaiting_deploy": lambda j: False}[job["state"]](job)

    def _branch_status(self, branch: str | None) -> str:
        """'merged', 'open' or 'gone' for a branch relative to the default branch."""
        if not branch or not git(self.repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}",
                                 check=False).strip():
            return "gone"
        tip = git(self.repo, "rev-parse", f"refs/heads/{branch}").strip()
        ok = run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", tip, self.cfg.default_branch],
                 shell=False).ok
        return "merged" if ok else "open"

    def _merge_time(self, branch: str) -> float | None:
        """When the branch reached the default branch: the oldest commit on the default branch
        that contains its tip (the merge commit, or the tip itself for a fast-forward)."""
        tip = git(self.repo, "rev-parse", f"refs/heads/{branch}", check=False).strip()
        if not tip:
            return None
        out = git(self.repo, "log", "--ancestry-path", "--format=%ct", f"{tip}..{self.cfg.default_branch}",
                  check=False).split()
        if out:
            return float(out[-1])
        return float(git(self.repo, "log", "-1", "--format=%ct", tip).strip() or 0) or None

    def _review_settled(self, job: dict[str, Any]) -> bool:
        """Notice a human merge (however it was done) or an abandoned branch."""
        status = self._branch_status(job["data"].get("branch"))
        if status == "merged":
            when = self._merge_time(job["data"]["branch"]) or time.time()
            # git times have one-second resolution; a merge can't precede its own delivery
            when = max(when, job["data"].get("delivered_at") or 0)
            self.store.update_job(job["id"], "merged", merged_at=when, merged_by="human")
            self.store.event(job["id"], "job.merged", "human", {"branch": job["data"].get("branch")})
            return True
        if status == "gone":
            self.store.update_job(job["id"], "rejected", outcome="abandoned: branch deleted without merging")
            return True
        return False

    def begin_review_merge(self, job_id: str, by: str = "human") -> None:
        """Move a reviewed job into merge execution so the UI can show immediate progress."""
        job = self.store.job(job_id)
        if job["state"] != "awaiting_review":
            raise ValueError(f"{job_id} is {job['state']}; only jobs awaiting review can be merged")
        self.store.update_job(job_id, "merging", merge_requested_by=by)
        self.store.event(job_id, "job.merge_requested", "human", {"by": by})

    def merge_reviewed(self, job_id: str, by: str = "human") -> str:
        """A human approves a delivered job: merge it now, then verify the merge."""
        self.begin_review_merge(job_id, by=by)
        return self.run_job(job_id)

    def reject_job(self, job_id: str, by: str = "human", reason: str = "") -> None:
        """A human gives up on a job. Its branch is kept; the work counts as waste, not output."""
        job = self.store.job(job_id)
        if job["state"] in ("done", "rejected"):
            raise ValueError(f"{job_id} is already {job['state']}")
        self.store.approve(job_id, by, "reject", reason)
        self.store.update_job(job_id, "rejected", outcome=f"rejected by {by}: {reason}".strip(": "))
        self.store.event(job_id, "job.rejected", "human", {"by": by, "reason": reason})

    def _s_merging(self, job: dict[str, Any]) -> None:
        by = job["data"].get("merge_requested_by")
        merged = self._delivery_for(job).merge(job, job["data"].get("delivery", {}))
        if by:
            self.store.approve(job["id"], by, "approve", "merged from review")
        self.store.update_job(job["id"], "merged", merged=merged, merged_at=time.time(),
                              merged_by=by or "agent", merge_requested_by="")
        self.store.event(job["id"], "job.merged", "human" if by else "factory", {"by": by or "agent"})

    def _s_merged(self, job: dict[str, Any]) -> None:
        """Merged is not done: the default branch must still pass, and the deploy gate must hold."""
        failure = self._post_merge_checks()
        if failure:
            # failed_state: a retry re-runs the checks; it must not rebuild work already merged.
            self.store.update_job(job["id"], post_merge={"status": "fail", "detail": failure[:4000]},
                                  failed_state="merged")
            return self._needs_human(job, f"post-merge check failed on {self.cfg.default_branch}: "
                                          f"{failure[:600]}")
        self.store.update_job(job["id"], post_merge={"status": "pass"})
        steps = self.deploy_steps(job)
        if not steps:
            return self._ship(job)
        names = [st["name"] for st in steps]
        if any(st.get("approval", "owner") == "owner" for st in steps):
            tip = git(self.repo, "rev-parse", self.cfg.default_branch).strip()
            preview = self._deploy_preview(steps, job, tip)
            self.store.update_job(job["id"], "awaiting_deploy",
                                  deploy={"steps": names, "status": "awaiting", "sha": tip, "preview": preview})
            self.notify("awaiting_deploy", self.store.job(job["id"]),
                        f"merged and verified; ready to deploy ({', '.join(names)})")
            return
        self.store.update_job(job["id"], "deploying", deploy={"steps": names, "status": "auto"})

    def _ship(self, job: dict[str, Any]) -> None:
        """Post-deploy gate, then done."""
        job = self.store.job(job["id"])
        result: GateResult = self.observe.check(job, job["data"].get("merged_at", time.time()))
        self.store.update_job(job["id"], post_deploy=result.__dict__)
        if result.status == "fail":
            return self._needs_human(job, f"post-deploy gate failed: {result.detail}")
        deployed = " deployed," if job["data"].get("deploy", {}).get("status") == "pass" else ""
        self.store.update_job(job["id"], "done", outcome="shipped", done_at=time.time())
        self._finish(job, f"shipped: merged, post-merge checks pass,{deployed} post-deploy {result.status}")

    _s_observing = _s_merged  # jobs saved before the rename

    # ------------------------------------------------------------------ deploy --
    def _job_paths(self, job: dict[str, Any]) -> list[str] | None:
        """Files the job changed, from its base commit to its last commit (None if unknown)."""
        base = (job["data"].get("sandbox") or {}).get("base_sha")
        commit = job["data"].get("commit")
        if not base or not commit:
            return None
        out = run(["git", "-C", str(self.repo), "diff", "--name-only", base, commit], shell=False)
        return [f for f in out.stdout.splitlines() if f] if out.ok else None

    def deploy_steps(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        """The deploy steps this job triggers: every step whose `when` paths it touched
        (a step without `when` always runs; unknown paths count as touching everything)."""
        changed = self._job_paths(job)
        steps = []
        for st in self.cfg.get("deploy.steps") or []:
            when = st.get("when")
            if when and changed is not None and not any(any_match(f, when) for f in changed):
                continue
            steps.append(st)
        return steps

    def _deploy_env(self, job: dict[str, Any], tip: str) -> dict[str, str]:
        # Deploy commands are written by the owner in factory.yaml (agents cannot edit it), and
        # they need real credentials (git push, cloud CLIs), so they get the full environment.
        env = {**os.environ, **{k: str(v) for k, v in (self.cfg.get("execution.env") or {}).items()}}
        env.update({"ASF_REPO": str(self.repo), "ASF_SHA": tip, "ASF_JOB": job["id"],
                    "ASF_JOB_TITLE": self._item(job).title,
                    "ASF_CHANGED_PATHS": "\n".join(self._job_paths(job) or [])})
        return env

    def _in_main_worktree(self, tip: str, fn: Callable[[Path], Any]) -> Any:
        tmp = Path(tempfile.mkdtemp(prefix="asf-deploy-"))
        wt = tmp / "main"
        git(self.repo, "worktree", "add", "--detach", "-q", str(wt), tip)
        try:
            return fn(wt)
        finally:
            run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(wt)], shell=False)
            shutil.rmtree(tmp, ignore_errors=True)

    def _deploy_preview(self, steps: list[dict[str, Any]], job: dict[str, Any], tip: str) -> str:
        """Output of each step's `preview` command (what would go live), for the approval."""
        cmds = [(st["name"], st["preview"]) for st in steps if st.get("preview")]
        if not cmds:
            return ""

        def go(wt: Path) -> str:
            parts = []
            for name, cmd in cmds:
                res = run(cmd, cwd=wt, timeout=int(self.cfg.get("execution.command_timeout_s", 900)),
                          env=self._deploy_env(job, tip))
                parts.append(f"## {name}" + ("" if res.ok else f" (preview exited {res.code})") +
                             f"\n{res.tail(3000)}")
            return "\n\n".join(parts)
        return self._in_main_worktree(tip, go)

    def approve_deploy(self, job_id: str, by: str = "owner") -> list[str]:
        """The owner's go-ahead. One deploy of the default branch covers every job waiting for
        it, so they are all approved together; returns their ids."""
        job = self.store.job(job_id)
        if job["state"] != "awaiting_deploy":
            raise ValueError(f"{job_id} is {job['state']}; only jobs awaiting deploy can be deployed")
        ids = [j["id"] for j in self.store.jobs(["awaiting_deploy"])]
        for jid in ids:
            d = self.store.job(jid)["data"].get("deploy") or {}
            self.store.update_job(jid, "deploying", deploy={**d, "status": "approved", "approved_by": by,
                                                            "approved_at": time.time()})
            self.store.event(jid, "deploy.approved", "human", {"by": by, "with": ids})
        return ids

    def _s_awaiting_deploy(self, job: dict[str, Any]) -> None:
        return None  # the owner approves with approve_deploy

    def _s_deploying(self, job: dict[str, Any]) -> None:
        """Run the job's deploy steps on the default branch tip. Jobs deployed by the same run
        share its result, so a batch deploys once."""
        tip = git(self.repo, "rev-parse", self.cfg.default_branch).strip()
        cache = self.__dict__.setdefault("_deploy_cache", {})
        results = []
        for st in self.deploy_steps(job):
            key = (tip, st["name"])
            if key not in cache:
                self.echo(f"[{job['id']}] deploy step {st['name']} at {tip[:10]}")
                cache[key] = self._in_main_worktree(tip, lambda wt, st=st: self._run_step(st, job, tip, wt))
            results.append(cache[key])
            if cache[key]["status"] == "fail":
                d = job["data"].get("deploy") or {}
                self.store.update_job(job["id"], deploy={**d, "status": "fail", "sha": tip, "results": results},
                                      failed_state="deploying")
                return self._needs_human(job, f"deploy step {st['name']} failed:\n{cache[key]['output'][-600:]}")
        d = job["data"].get("deploy") or {}
        self.store.update_job(job["id"], deploy={**d, "status": "pass", "sha": tip, "results": results,
                                                 "deployed_at": time.time()})
        self._ship(job)

    def _run_step(self, st: dict[str, Any], job: dict[str, Any], tip: str, wt: Path) -> dict[str, Any]:
        started = time.time()
        res = run(st["run"], cwd=wt, timeout=int(st.get("timeout_s", 1800)), env=self._deploy_env(job, tip))
        self.store.event(job["id"], "deploy.step", "factory",
                         {"step": st["name"], "ok": res.ok, "sha": tip, "output": res.tail(4000)})
        return {"name": st["name"], "status": "pass" if res.ok else "fail", "sha": tip,
                "seconds": round(time.time() - started, 1), "output": res.tail(4000)}

    def _post_merge_checks(self) -> str:
        """Run observe.post_merge commands on the default branch tip in a throwaway worktree.
        Returns '' when they pass (or none are configured), else the failing output."""
        commands = self.cfg.get("observe.post_merge") or []
        if not commands:
            return ""
        tip = git(self.repo, "rev-parse", self.cfg.default_branch).strip()
        cache = self.__dict__.setdefault("_post_merge_cache", {})
        if tip in cache:  # several jobs merged onto the same commit: check it once
            return cache[tip]
        cache[tip] = self._run_post_merge(commands)
        return cache[tip]

    def _run_post_merge(self, commands: list[str]) -> str:
        tmp = Path(tempfile.mkdtemp(prefix="asf-postmerge-"))
        wt = tmp / "main"
        git(self.repo, "worktree", "add", "--detach", "-q", str(wt), self.cfg.default_branch)
        try:
            # A bare checkout has no dependency folders (e.g. .venv): give it what sandboxes get,
            # or commands like `.venv/bin/python -m pytest` cannot run at all.
            self.sandboxes.prepare(wt)
            for cmd in commands:
                res = run(cmd, cwd=wt, timeout=self.cfg.get("execution.command_timeout_s", 900),
                          env=sandbox_env(self.cfg.get("execution.env")))
                if not res.ok:
                    return f"`{cmd}` failed:\n{res.tail(3000)}"
            return ""
        finally:
            run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(wt)], shell=False)
            shutil.rmtree(tmp, ignore_errors=True)

    def migrate_legacy(self) -> int:
        """Jobs saved when 'done' meant 'delivered' become awaiting_review; returns how many."""
        n = 0
        for job in self.store.jobs(["done"]):
            if job["data"].get("outcome") == "delivered for human merge":
                self.store.update_job(job["id"], "awaiting_review", outcome="", migrated=True,
                                      delivered_at=job["data"].get("delivered_at") or job["updated"])
                n += 1
        return n

    def _finish(self, job: dict[str, Any], message: str) -> None:
        job = self.store.job(job["id"])
        self.notify("done", job, message)
        self._release_sandbox(job)

    def _release_sandbox(self, job: dict[str, Any]) -> None:
        if not self.cfg.get("execution.keep_sandboxes", False):
            try:
                self._sandbox(self.store.job(job["id"])).destroy()
            except Exception:  # noqa: BLE001
                pass


def _real_question(q: Any) -> bool:
    """Models sometimes fill an optional field with a placeholder such as '""', 'none' or 'N/A'."""
    if not isinstance(q, str):
        return bool(q)
    t = q.strip().strip('"\'` ').lower()
    return t not in ("", "none", "null", "n/a", "na", "no", "-")


def owner_notes(summary: str) -> str:
    """The text after 'OWNER REVIEW:' in an agent summary, up to the next blank line pair."""
    i = summary.find("OWNER REVIEW:")
    if i < 0:
        return ""
    rest = summary[i + len("OWNER REVIEW:"):]
    end = rest.find("\n\n\n")
    return (rest if end < 0 else rest[:end]).strip()
