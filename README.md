# asf: Agentic Software Factory

Work items in, verified branches and pull requests out. This is the implementation of
[the design](design/DESIGN.md): agents do the engineering
labor inside sandboxes, deterministic gates and independent reviewers decide what is
trustworthy, and a single `factory.yaml` that the team owns sets the trust boundary.

```
work item ─▶ triage ─▶ implementer ─▶ independent tester ─▶ deterministic gates ─▶ reviewer + security
 (beads, GitHub,        (cheap model)   (sandbox, broker)    (sees diff only)      (base-compared)       (cold read)
  Linear, Jira, Slack,                                                                   │
  Sentry, PagerDuty,                                                         CI fixer ◀──┘ on failure
  local inbox)
                     ─▶ commit ─▶ deliver (branch / draft PR) ─▶ [approval] ─▶ merge ─▶ post-deploy gate
```

For a worked example of how the factory was built and what it did on a real backlog, see
[docs/HOW-IT-WAS-BUILT.md](docs/HOW-IT-WAS-BUILT.md).

## Quick start for a new repository

```bash
pip install -e .                                   # or: pip install asf
asf --repo ~/code/myapp init                       # drafts factory.yaml, commits it on branch factory/setup
asf --repo ~/code/myapp --env-file .env doctor     # checks commands, adapters, credentials
asf --repo ~/code/myapp intake                     # pulls ready work from the configured trackers
asf --repo ~/code/myapp run --all --stop-at triaged   # cheap: sort the backlog first
asf --repo ~/code/myapp run --job JOB-xxxxxx       # implement, test, gate, review, deliver
asf --repo ~/code/myapp status
asf --repo ~/code/myapp report
```

Nothing touches your checkout. Each job runs in its own git worktree under
`$ASF_HOME/<repo>/sandboxes/`, and the result is a branch in your repository plus a
pull-request description in `$ASF_HOME/<repo>/reports/`. Shadow mode is on by default,
so nothing merges until you raise `autonomy.max_merge_tier` in a reviewed change to
`factory.yaml`.

Zero-infrastructure mode on GitHub: `asf ci-workflow --write` generates a workflow that
runs the whole factory inside GitHub Actions when an issue gets the `factory` label.

## The console

```bash
asf ui --open            # every project under ~/.asf, at http://127.0.0.1:8787
```

One local page for every project the factory works on (bound to 127.0.0.1, with a per-process
token on every action).

- **Home:** one combined inbox of everything waiting on you across projects, oldest first,
  and a card per project. Each card shows what needs you and how long the oldest has waited,
  agent work in progress, shipped this week, cost per shipped job, waste, policy and last
  activity.
- **Project view** (`#p/<project>`): needs you, working, backlog with `depends_on` resolved,
  history, and per job the reason, owner-review lines, findings, diff, report and timeline.
  Every owner action is a button: run the queue, merge, send back with notes, reject, resume,
  retry, start over, queue a backlog item, pause.
- A reconciler notices merges made outside the factory, in any project, and verifies them.

Projects are found automatically: every state directory under `$ASF_HOME` (default `~/.asf`)
with a `factory.db`. The repo path is recorded in `project.json` whenever asf opens a project,
or recovered for older state. `~/.asf/projects.yaml` is optional and lets you name, order,
prioritise, hide or archive projects:

```yaml
projects:
  workflow-engine-11bf9a: {name: Workflow engine, priority: 1}
  CycleSense-98c570: {archived: true}
```

## The scheduler: one queue for every project

All projects share one model subscription, so one scheduler runs their jobs. Start and stop it,
and switch its behaviour, from the console's home page or in `~/.asf/projects.yaml`:

```yaml
scheduler: {mode: priority, concurrency: 1, auto_resume: true}
projects:
  workflow-engine-11bf9a: {priority: 1}   # lower runs first
```

| Mode | Behaviour |
|---|---|
| `priority` | Strict priority: the highest-priority project's work runs until it has nothing runnable; lower-priority projects wait. |
| `combined` | Interleaved: jobs are taken from every project in turn (priority breaks ties), so all keep moving. |
| `separate` | Each project runs on its own, as if it had its own factory; none waits for another. |

In every mode at most `concurrency` jobs run at once, and within a project the work furthest
along (verifying, reviewing) goes before new work. A usage-limit error ("You've hit your
5-hour limit · resets 1pm (Asia/Karachi)") pauses all model work until the stated reset; with
`auto_resume`, the jobs it interrupted continue where they stopped.

## Job states: delivered is not done

```
queued -> triaged -> implementing -> testing -> verifying -> reviewing -> committing -> delivering
  -> awaiting_review (a person reviews; or awaiting_approval -> merging when the agent may merge)
  -> merged   (on the default branch: observe.post_merge commands run there, then the deploy gate)
  -> done     (shipped: the only state that counts as output)
```

The factory notices a merge however it was made, and a deleted branch counts as abandoned.
`asf report` (and the console) count only shipped work: cost per shipped job is all spend
divided by shipped jobs, abandoned spend is reported as waste, and lead time is split into
agent time, review wait and post-merge verification.

**Owner review.** Paths only the owner approves (`owner_review.paths`, else every tier-3 rule)
are pulled out at commit time with exactly their diff and the agent's `OWNER REVIEW:` notes.
The report and the console lead with them, so the owner reviews a small diff instead of
writing the change.

## Commands

| Command | What it does |
|---|---|
| `init` | Setup agent: detects stacks, packages, test/lint/typecheck/build commands, dependency folders, risk-sensitive paths and trackers. Writes a pending config and commits it on `factory/setup` through a temporary worktree. |
| `doctor` | Validates config, checks every command's executable, adapters and credentials. |
| `intake [--source S] [--id ID] [--rerun]` | Pulls work items from enabled intake adapters. Deduplicates by source and id: an item that already has a job, finished or not, is skipped, so a labelled issue is never rebuilt by accident. `--rerun` starts a new job for an item whose last job has ended. |
| `add --title ... [--accept ...]` | Queues a work item by hand. |
| `run [--job ID] [--all] [--stop-at STATE]` | Drives jobs through the state machine. Resumable after a crash. |
| `status [JOB]`, `events JOB [--jsonl]`, `report [--json]` | Job list, full audit trail of every agent tool call, metrics that count only shipped work. |
| `ui [--port N] [--open]` | The local web console for all projects (see above); with `--repo`, it opens that project. |
| `approve` / `reject JOB` | Human approval for tiers that require it. |
| `resume JOB [--extra-usd N]`, `retry JOB` | Continue a budget-paused job, or re-enter a failed one from a clean sandbox. |
| `revise JOB --note "..." [--notes-file F] [--extra-usd N]` | Send a delivered, unmerged job back with review notes. The implementer works on top of the delivered commit on the same branch, then the tester, gates and reviewers run again over the whole change, and one new commit is added. Each revision gets a fresh budget. Follow with `run --job JOB`. |
| `publish JOB` | Push a delivered job's branch and open a draft pull request (the report, plus `Fixes #N` for GitHub issues). Works whatever `delivery.provider` is; later revisions of the job update the same PR. |
| `reviews [--job JOB] [--dry-run]` | Review on GitHub: a comment starting with `/revise` on a factory PR sends the job back with notes (the rest of the comment plus every line comment on the diff since the last revision), pushes the new commit to the PR and replies with the result. Only the repo owner, members and collaborators are obeyed; other comments are ignored and logged. Run it by hand or on a schedule. |
| `pause-all` / `resume-all` | Kill switch. A `PAUSED` file in the state directory works too. |
| `serve [--worker]` | Webhook gateway with signature verification, job API, optional worker loop. |
| `ci-workflow [--write]` | GitHub Actions workflow for runner mode. |
| `adapters` | Lists installed adapters, including third-party ones registered by entry point. |

Global flags: `--repo`, `--config`, `--state`, `--env-file`, and `--set key=value` to
override any config value (CI uses this to switch delivery to GitHub).

## Design-to-code map

| Design component | Code | Status |
|---|---|---|
| Normalized `WorkItem`, job state machine | `asf/models.py`, `asf/orchestrator.py` | Built, tested |
| Durable orchestration with budgets and kill switch | `asf/orchestrator.py`, `asf/store.py` (SQLite, WAL) | Built, tested. SQLite stands in for Temporal plus Postgres. |
| Policy engine: tiers, gates, approvals, merge rights | `asf/policy.py`, `asf/config.py` | Built, tested |
| Tool broker: role permissions, protected paths, test integrity, command allowlist, secret redaction | `asf/broker.py` | Built, tested |
| Agent roles: triage, implementer, tester, reviewer, security, CI fixer | `asf/agents/` | Built, live-tested with Claude |
| Deterministic gates with base-commit comparison | `asf/gates.py` | Built, tested |
| Setup agent | `asf/setup_agent.py` | Built, tested on Node monorepo, Python, Go fixtures and two real repos |
| Webhook gateway | `asf/server.py` | Built, tested with signed requests |
| Intake: local inbox, beads | `adapters/intake/` | Live-tested |
| Intake: GitHub Issues, Linear, Jira, Slack, Sentry, PagerDuty | `adapters/intake/` | Built; webhook parsing and signatures unit-tested; not run against live accounts |
| Sandbox: local worktree | `adapters/sandbox/local_worktree.py` | Live-tested |
| Sandbox: Docker, no network | `adapters/sandbox/docker.py` | Built, command construction unit-tested; not run live |
| Runner mode in GitHub Actions | `asf/ci.py`, `templates/github-actions.yml` | Generated and YAML-validated; not run live |
| Delivery: local branch | `adapters/delivery/local_git.py` | Live-tested, including merge-after-approval |
| Delivery: GitHub PR, GitLab MR | `adapters/delivery/` | Built; GitHub unit-tested with a fake runner (open or update a PR, read PR comments, reply); GitLab not run live |
| Notify: console, Slack webhook. Observe: Sentry post-deploy gate | `adapters/notify/`, `adapters/observe.py` | Built; Slack and Sentry not run live |
| Models: Anthropic with caching, effort per role, refusal fallbacks | `adapters/models/anthropic_provider.py` | Live-tested |
| Models: Claude Code headless (`claude -p`), built-in tools off, tool calls as structured output, session resume | `adapters/models/claude_code.py` | Unit-tested with a fake CLI; live-tested on a two-turn tool loop |
| Not built yet | | Planner task-graph decomposition (triage flags epics instead), embedding code index and per-repo memory, OpenAI and Google model adapters, hosted microVM sandboxes, dashboard UI |

## Adding an adapter

Subclass the interface in `asf/adapters/base.py` and register it, either in `BUILTIN` or
from your own package:

```toml
[project.entry-points."asf.adapters"]
"intake.clickup" = "my_pkg.clickup:ClickUpIntake"
```

The core never imports vendor code, so a new tracker, sandbox, SCM or model provider is a
new adapter and a line in `factory.yaml`.

## Security model, as built

- Agents reach the sandbox only through the broker. A DENIED result is final, and every
  call, allowed or not, is logged with role, arguments and reason.
- Commands run with a scrubbed environment: no API keys, tokens or cloud credentials are
  inherited. Secret-looking files cannot be read, and tool output is redacted.
- `factory.yaml`, CI config and secret files are protected paths. Existing tests cannot be
  edited or deleted above tier 0, and the tester role can write only test files.
- The local worktree sandbox does not block network access, and tests execute code the
  agent wrote. Use the Docker provider or a CI runner where that matters.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The suite runs the full pipeline end to end with a scripted model. It covers shadow
delivery, merge after approval, reviewer fix rounds, budget pause and resume, the kill
switch, prompt-injection containment, crash recovery, retry from a clean sandbox, and the
signed webhook gateway. No network calls are made and nothing is spent.
