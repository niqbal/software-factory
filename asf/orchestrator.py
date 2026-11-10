"""The factory: a resumable job state machine over the adapters.

Each state handler does one step and moves the job forward. State is written to
the store after every step, so a crashed or interrupted run resumes where it
stopped with `asf run --job <id>`.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import policy
from .adapters import base as adapters
from .adapters.base import AdapterContext, ModelProvider, Sandbox
from .agents.roles import BudgetExceeded, PromptBuilder, run_agent, work_item_block
from .broker import ToolBroker
from .config import Config
from .gates import GateRunner
from .models import TERMINAL, AgentResult, GateResult, WorkItem
from .report import render_report
from .store import Store
from .util import git, run, slugify

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
    def run_job(self, job_id: str, stop_at: str | None = None) -> str:
        while True:
            job = self.store.job(job_id)
            state = job["state"]
            if state in TERMINAL or state == "paused" or state == stop_at:
                return state
            if state == "awaiting_approval" and not self._approvals_settled(job):
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

    def resume(self, job_id: str, extra_usd: float = 0.0) -> None:
        job = self.store.job(job_id)
        if job["state"] != "paused":
            raise ValueError(f"{job_id} is {job['state']}, not paused")
        self.store.update_job(job_id, job["data"].get("resume_state", "queued"),
                              extra_budget_usd=job["data"].get("extra_budget_usd", 0) + extra_usd)

    def retry(self, job_id: str) -> str:
        """Re-enter a failed or needs_human job at the step that stopped it."""
        job = self.store.job(job_id)
        if job["state"] not in ("failed", "needs_human"):
            raise ValueError(f"{job_id} is {job['state']}; only failed or needs_human jobs can be retried")
        target = job["data"].get("failed_state") or ("implementing" if job["data"].get("sandbox") else "queued")
        if target == "implementing" and job["data"].get("sandbox"):
            # A fresh attempt starts from the base commit, not from a half-finished one. During a
            # revision the base is the delivered commit: the work already on the branch is kept.
            revision = self._active_revision(job)
            self._sandbox(job).reset(revision["from_commit"] if revision else None)
            self.store.event(job_id, "sandbox.reset", "factory", {"reason": "retry"})
        self.store.update_job(job_id, target, error="", needs_human_reason="", feedback="",
                              tester_rounds=0, review_rounds=0, ci_rounds=0)
        return target

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
        if job["state"] != "done" or d.get("merged") or d.get("outcome") == "merged":
            raise ValueError(f"{job_id} is {job['state']}"
                             + (" and merged" if d.get("merged") or d.get("outcome") == "merged" else "")
                             + "; only delivered, unmerged jobs can be revised")
        branch = d.get("branch")
        if not branch or not git(self.repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}",
                                 check=False).strip():
            raise ValueError(f"{job_id}: branch {branch or '(none)'} no longer exists")
        tip = git(self.repo, "rev-parse", f"refs/heads/{branch}").strip()
        if run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", tip, self.cfg.default_branch],
               shell=False).ok:
            raise ValueError(f"{branch} is already merged into {self.cfg.default_branch}; "
                             "open a new work item for further changes")
        checked_out = [line.split(" ", 1)[1] for line in
                       git(self.repo, "worktree", "list", "--porcelain").splitlines()
                       if line.startswith("worktree ")]
        sandbox_root = (d.get("sandbox") or {}).get("root")
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

    def _active_revision(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """The revision in progress, if the job was sent back with `revise` and is not done yet."""
        revisions = job["data"].get("revisions") or []
        return revisions[-1] if revisions and job["state"] != "done" and not revisions[-1].get("commit") \
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

    def _needs_human(self, job: dict[str, Any], reason: str) -> None:
        self.store.update_job(job["id"], "needs_human", needs_human_reason=reason)
        self.notify("needs_human", self.store.job(job["id"]), reason)

    # -------------------------------------------------------------- states ---
    def _s_queued(self, job: dict[str, Any]) -> None:
        item = self._item(job)
        spec = self.cfg.role_model("triage")
        triage, turn = self.provider.structured(
            spec["model"], PromptBuilder(self.cfg, self.repo).system("triage", 1),
            work_item_block(item, item.acceptance), TRIAGE_SCHEMA, spec.get("effort", "low"))
        self._charger(job["id"])("triage", turn)
        pre = policy.pre_tier(self.cfg, item, triage.get("tier_guess"))
        acceptance = item.acceptance or triage.get("acceptance_criteria", [])
        self.store.event(job["id"], "triage", "triage", triage)
        self.store.update_job(job["id"], "triaged", tier=None, pre_tier=pre, triage=triage,
                              acceptance=acceptance, manual_verification=triage.get("manual_verification", []))
        if not triage.get("automatable", True):
            self._needs_human(self.store.job(job["id"]), f"not automatable: {triage.get('reason_if_not')}")
        elif triage.get("clarification_needed"):
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
        feedback = job["data"].get("feedback")
        if feedback:
            text += "\n\nThe previous attempt needs changes. Address these findings:\n" + feedback
        return text + (("\n\n" + extra) if extra else "")

    def _s_implementing(self, job: dict[str, Any]) -> None:
        tier = self._current_tier(job)
        res = self._agent(job, "implementer", self._task(job), tier)
        if res.data.get("blocked_reason"):
            return self._needs_human(job, f"implementer blocked: {res.data['blocked_reason']}")
        sb = self._sandbox(job)
        changed = sb.uncommitted_files() if self._active_revision(job) else sb.changed_files()
        if not changed:
            return self._needs_human(job, f"implementer made no changes: {res.summary[:500]}")
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
        job = self.store.job(job["id"])
        report = render_report(job, item, self.store.runs(job["id"]), self.store.job_cost(job["id"]))
        path = self.state_dir / "reports" / f"{job['id']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report)
        self.store.update_job(job["id"], "delivering", commit=sha, report=str(path))

    def _s_delivering(self, job: dict[str, Any]) -> None:
        d = job["data"]["decision"]
        item = self._item(job)
        report = Path(job["data"]["report"]).read_text()
        draft = bool(self.cfg.get("delivery.draft", True)) or not d["may_agent_merge"]
        result = self.delivery.deliver(job, item, self._sandbox(job), report, draft=draft)
        self.store.update_job(job["id"], delivery=result)
        if not d["may_agent_merge"]:
            self.store.update_job(job["id"], "done", outcome="delivered for human merge")
            self._finish(job, f"delivered {result.get('url') or result.get('branch')} ({d['reason']})")
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

    def _s_merging(self, job: dict[str, Any]) -> None:
        merged = self.delivery.merge(job, job["data"].get("delivery", {}))
        self.store.update_job(job["id"], "observing", merged=merged, merged_at=time.time())

    def _s_observing(self, job: dict[str, Any]) -> None:
        result: GateResult = self.observe.check(job, job["data"].get("merged_at", time.time()))
        self.store.update_job(job["id"], post_deploy=result.__dict__)
        if result.status == "fail":
            return self._needs_human(job, f"post-deploy gate failed: {result.detail}")
        self.store.update_job(job["id"], "done", outcome="merged")
        self._finish(job, f"merged; post-deploy: {result.status}")

    def _finish(self, job: dict[str, Any], message: str) -> None:
        job = self.store.job(job["id"])
        self.notify("done", job, message)
        if not self.cfg.get("execution.keep_sandboxes", False):
            try:
                self._sandbox(job).destroy()
            except Exception:  # noqa: BLE001
                pass
