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

## Set up in five minutes

You need Python 3.11+, git, and one signed-in model backend: Claude Code (`claude auth login`),
Cursor (`cursor-agent login`), or an Anthropic API key. Everything else is optional.

```bash
scripts/install.sh                          # venv, asf on PATH, machine check
asf setup                                   # wizard: machine, add a project, first work item
asf ui --setup                              # or the same flow in the browser
```

`install.sh --project ~/code/myapp` adds a project in the same run, `--ui` opens the console
afterwards, and `--with-e2e` adds Playwright for browser checks. In scripts, use flags:

```bash
asf setup check [--json]                                   # exit 0 when the machine is ready
asf setup add ~/code/myapp --name "My app" --priority 2    # detect, draft, register, check
asf setup add ~/code/myapp --backend pool --github-issues --label factory --github-prs
asf setup add ~/code/myapp --print                         # show the draft, write nothing
```

`setup add` drafts `factory.yaml` in shadow mode, commits it on `factory/setup` without touching
your checkout, registers the project in the console, and runs the same checks as `doctor`.
Where the factory fits among other agent tools, why its code is worse than an interactive Claude
session today, and the plan: [docs/PARITY.md](docs/PARITY.md).

## Quick start for a new repository

Step-by-step, for new and existing repositories: [docs/ADD-A-PROJECT.md](docs/ADD-A-PROJECT.md).

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

## Model backends: Claude Code, Cursor, or both

Agents reach models through the CLIs you are already signed in to, so work runs on your
subscriptions rather than API keys:

| `models.provider` | What runs each agent turn |
|---|---|
| `claude_code_native` | The implementer, tester and CI fixer run as real Claude Code sessions in the job's worktree (see below); reviewers and triage as `claude_code` |
| `claude_code` | `claude -p` with its own tools switched off (`--tools ""`) and structured output |
| `cursor_native` | The same for Cursor: the writing roles run as real `cursor-agent` sessions in the worktree; reviewers and triage as `cursor_cli` |
| `cursor_cli` | `cursor-agent -p --mode ask --sandbox enabled` in a scratch folder whose `.cursor/cli.json` denies all of Cursor's own tools |
| `pool` | Several backends with a per-role preference order and failover |
| `anthropic` | The Anthropic API (needs a key) |

With the emulated providers the model only *requests* tool calls (as structured output or
JSON), and asf's broker executes them, so permissions, protected paths and test integrity hold
no matter which model is working.

`claude_code_native` gives the writing roles what an interactive session has: Claude Code's own
system prompt (the role prompt is appended to it), its own Read/Edit/Write/Glob/Grep/Bash,
subagents and skills, the repository's `CLAUDE.md` files, one continuous context and a real
shell. The broker still decides: a `PreToolUse` hook sends every tool call to the factory, which
applies the same path, secret, protected-file, test-integrity and command rules; `run_check`
and `finish` come from a small asf MCP server backed by the same broker; files a shell command
changed are checked after the session and put back when the role may not change them. Network
tools, installs and git writes are refused, and the job's remaining dollars and minutes are
enforced inside the session (`--max-budget-usd`). A fix round resumes the implementer's own
session. It needs a sandbox whose checkout is the boundary (`local_worktree`); with Docker the
roles fall back to `claude_code`. Settings: `native_roles`, `session_max_turns` (150),
`session_timeout_s` (3600), plus the `claude_code` ones. A pool can use it as a backend.

`cursor_native` does the same with Cursor's agent, through Cursor's `preToolUse` hook and its
MCP support, with its OS sandbox on for shell commands (`sandbox: enabled`). Cursor reads hooks
and MCP servers only from the workspace, so asf writes `.cursor/hooks.json`, `mcp.json` and
`cli.json` into the checkout for each session and puts back whatever was there afterwards; the
agent can neither read nor change them. Cursor gives up on an MCP call after about a minute, so
a longer `run_check` reports that it is still running and the agent waits with `check_result`.
Cursor has no in-session budget or turn cap: the wall clock bounds a session, and spend is the
`usd_per_mtok` estimate, as with `cursor_cli`. The shared machinery is in
`adapters/models/native.py`.

A pool:

```yaml
models:
  provider: pool
  backends:
    cursor: {provider: cursor_cli, default: {model: gpt-5.3-codex}, roles: {implementer: {model: gpt-5.3-codex-high}}}
    claude: {provider: claude_code, default: {model: claude-opus-5-5}, roles: {reviewer: {model: claude-opus-5-5, effort: high}}}
  routing:
    default: [cursor, claude]     # first usable backend wins
    reviewer: [claude, cursor]    # a different model family reviews the work
```

Usage limits are tracked per backend (`~/.asf/limits.json`, shared by all projects). A
Claude limit no longer stops a pooled project: the interrupted job retries on Cursor, and the
reverse. `asf doctor` checks every backend and prints the routing, and the console shows each
backend's status and which backend ran each role of a job.

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
queued -> triaged -> [reproducing] -> implementing -> testing -> verifying -> [confirming] -> reviewing -> committing -> delivering
  -> awaiting_review (a person reviews; or awaiting_approval -> merging when the agent may merge)
  -> merged   (on the default branch: observe.post_merge commands run there)
  -> awaiting_deploy -> deploying   (only when a deploy step matches the change)
  -> done     (shipped: merged, verified, deployed, post-deploy gate held; the only output)
```

The factory notices a merge however it was made, and a deleted branch counts as abandoned.
`asf report` (and the console) count only shipped work: cost per shipped job is all spend
divided by shipped jobs, abandoned spend is reported as waste, and lead time is split into
agent time, review wait and post-merge verification.

**Deploy.** `deploy.steps` in `factory.yaml` are what "in production" means for a project.
After the post-merge checks pass, every step whose `when` paths the job touched runs on a
clean worktree of the default branch tip, with the full environment plus `ASF_REPO`,
`ASF_SHA`, `ASF_JOB`, `ASF_JOB_TITLE` and `ASF_CHANGED_PATHS`. `approval: owner` (the
default) parks the job as **ready to deploy** with the step's `preview` output; the owner
clicks Deploy in the console (or runs `asf deploy JOB`), and one deploy covers every job
waiting for it. `approval: auto` deploys straight away. A failed step stops the job for a
human; `retry` runs the deploy again. A change no step matches is done after its checks.

```yaml
deploy:
  steps:
  - name: publish-site
    when: [web/**]                       # paths that need a deploy; omit to deploy every merge
    preview: scripts/what-would-change   # shown with the approval
    run: scripts/deploy "$ASF_SHA"
    approval: owner                      # owner | auto
    timeout_s: 600
```

Deploy commands run owner-written code with real credentials, so protect the scripts they
call (`permissions.protected_paths`) as well as `factory.yaml`.

**Owner review.** Paths only the owner approves (`owner_review.paths`, else every tier-3 rule)
are pulled out at commit time with exactly their diff and the agent's `OWNER REVIEW:` notes.
The report and the console lead with them, so the owner reviews a small diff instead of
writing the change.

For the `test` gate, coverage is path-based: every changed code file must be owned by at least one package that declares a `test` command. At tiers 1-3, uncovered code paths fail the gate and list the files; at tier 0, the gate passes with a warning that lists those files. Docs and config-only changes are not treated as code by this rule.

## Browser reproductions: prove the bug before the fix, and prove it gone after

Tests that read source text or run in a fake DOM can pass while the real page is still broken.
For a bug that shows in the app (or a feature a user can see), the factory proves the problem in
a real browser on the base commit before anyone fixes it, and proves it gone afterwards, in two
ways that must agree.

**`app:`** in `factory.yaml` says how to start the app. The factory starts it in the checkout
being checked (the job's sandbox, the base commit, a post-merge worktree) with the sandbox's
scrubbed environment, waits for the health check, and stops the whole process group afterwards.

```yaml
app:
  start: .venv/bin/python -m asf.demo --port {port}   # {port} is replaced; PORT is exported too
  port: 0                  # 0: a free port each time
  health: /api/health      # path (or URL) polled until it answers 2xx/3xx
  start_timeout_s: 30
packages:
- name: asf
  commands:
    e2e: .venv/bin/python -m pytest -q -p no:cacheprovider {args}   # {args}: tests/e2e or one test file
    repro: tests/repro     # not a command: the folder of English reproductions
```

This repository's app is `python -m asf.demo`: the console with demo data (one job with a long
diff, at `/#p/demo/JOB-demo01`), built in a temporary folder from the checkout under test.

**Two suites, both committed.**

| Suite | File | Run by | When |
|---|---|---|---|
| English reproduction | `tests/repro/<slug>.md`: front matter `item`, `kind` (bug / feature), `covers:` path globs, `path` to open, `test`; then numbered steps, the expected result, the values to measure | the reproducer agent, in a real browser (model time) | the job that wrote it; later jobs whose changed files match `covers:`; `asf repro --all` |
| e2e test | `tests/e2e/test_<slug>.py` (pytest-playwright; the app's URL is `ASF_APP_URL`) | pytest, deterministic | the `e2e` gate on every change, after every merge, `asf e2e` |

`tests/e2e/conftest.py` skips the e2e suite when `ASF_APP_URL` is unset, so a plain `pytest`
needs no browser. The e2e suite needs `pip install -e '.[e2e]'` and `playwright install chromium`
in the environment the factory runs commands in (here `.venv`); the reproducer needs `npx`.

**The reproducer** is `claude -p` with every built-in tool switched off (`--tools ""`) and one MCP
server, `@playwright/mcp` (`--mcp-config` with `--strict-mcp-config`), headless and limited to the
app's origin. `--allowedTools` lists the browser tools it may use, `--permission-mode dontAsk`
refuses the rest, and the two that reach outside the browser (`browser_run_code_unsafe`, which
runs code in the MCP server's Node process, and `browser_file_upload`, which reads local files)
are removed with `--disallowedTools`. So it can drive a browser and nothing else: no files, no
shell. It answers with `--json-schema` structured output (reproduced, measured values, notes,
screenshot names, the test source), and the factory writes the files: the broker stays the only
path from a model to the repository. Screenshots go to `$ASF_HOME/<repo>/evidence/<job>/`.
Settings live under `repro:` (`model`, `effort`, `timeout_s`, `mcp_command`, `e2e_dir`).

**The flow**, for a bug or feature when an app is configured and triage wrote browser steps:

1. Triage writes the English steps (`repro` in its output): path, numbered steps, expected
   result, values to measure, the paths they cover.
2. `reproducing`: on the base commit the reproducer runs the steps and must reproduce the
   problem (for a feature: show the behaviour missing). If it can't, the job stops with
   "bug not understood" (or "feature not understood"), asked on the issue; an answer re-triages
   with it.
3. The reproducer writes the e2e test from what it saw. It must fail on base *by its assertion*;
   a pass, a timeout, a missing selector or a crash proves nothing. One regeneration with the
   pytest output, then "repro test unusable" for a human.
4. Both files are committed on the branch before the implementer starts. For the rest of the job
   they are protected: the broker refuses edits, and the `test_integrity` gate fails if they
   changed. If the fix must change what the test relies on (selectors, page structure), the
   implementer stops with "repro test needs changing: ..." and the owner decides (**Allow test
   change** in the console, or `asf allow-repro-edit JOB`). An approved edit must still fail by
   its assertion on the code before the fix.
5. After the fix the `e2e` gate must pass, then `confirming`: the job's own test runs again and
   the reproducer runs the steps on the branch and must report the problem gone. If the two
   disagree (or both still see it), that is a defect with evidence: one fix round with that
   evidence, then the job stops with "fix not confirmed". In the same step, every committed
   reproduction whose `covers:` matches the change's files is run and must stay gone.

The console's Overview tab shows the evidence: both verdicts on the base commit and after the
fix, the measured values, the reproducer's notes and its screenshots.

## Evals: is the factory's code as good as an interactive session?

`asf eval` measures the factory against tests it never sees, so every change to it (native
Claude Code or Cursor sessions, models and effort, prompts) is an A/B test on the same task set.
This is Phase 0 of [docs/PARITY.md](docs/PARITY.md).

- **A case** is a repository, a base commit, the ticket exactly as its requester wrote it, and
  the merged reference solution. The tests the reference added or changed are the case's
  **hidden tests**: the factory's checkout holds only the history up to the base commit, so
  neither it nor the baseline can find them.
- **A variant** is a named overlay on the case's `factory.yaml`: `current` (unchanged),
  `claude_code`, `claude_code_native`, `cursor_cli`, `cursor_native` (Grok 4.7), `opus-high`, or
  your own file in the archive's `variants/` folder.
  `interactive` is the baseline: plain `claude -p` with its default tools in the checkout, given
  the ticket text and nothing else.
- **A run** clones the case at its base commit, registers a throwaway project with the variant's
  config and takes the ticket to delivery through the real pipeline in shadow mode, delivering
  to a local branch: nothing merges and nothing is pushed. Then the hidden tests are written
  over the factory's branch and run.

Cases and results live only in the private runs archive (default `~/work/asf-runs/evals`, or
`$ASF_EVALS`, or `--evals`), never in this repository or a product repository; `asf eval`
refuses an archive inside either.

```bash
asf eval import --project myapp-1a2b3c                 # draft cases from the factory's done jobs
asf eval mine --repo ~/work/myapp --since 2026-01-01   # draft cases from history
# review each draft, move it to sets/<set>/cases/
asf eval check --set myapp                             # sort hidden tests: fail-to-pass, pass-to-pass, flaky
asf eval run --set myapp --config interactive --config claude_code --config claude_code_native --repeat 2
asf eval report --set myapp                            # tables, plus reports/latest.{md,json}
```

Cases come from three places. `import` turns jobs the factory finished into cases: the work item
exactly as its requester wrote it, the job's base commit, and the final commit you merged (after
any revisions), noting which backend wrote it. `mine` keeps first-parent commits and merged pull
requests that touch code and a runnable test, change under 400 lines, and reference an issue or
have a descriptive message; its tickets come from commit messages and need rewriting. And a case
can be authored: its own hidden tests (`hidden_dir`), its own reference (`reference_patch`, made a
commit in the eval mirror only), and `needs_app: true` when the hidden tests are browser tests run
against `app.start`.

Scoring is per test. `check` runs the hidden tests once on the base commit and twice on the
reference: tests that fail on base and pass on the reference are fail-to-pass (what the ticket
asks for), tests passing on both are pass-to-pass (must not break), and tests that change result
between the two reference runs are flaky and left out. A run resolves the case when every
fail-to-pass test passes and no pass-to-pass test breaks. pytest, go test, vitest, jest and
`node --test` results are read per test. When hidden tests fail because a name they use does not
exist in the candidate (a Go `undefined:`, an `ImportError`, a missing TypeScript export), the run
is counted as an interface mismatch and left out of the quality numbers rather than scored as a
wrong fix: name that interface in the case's acceptance criteria.

Each run records the final state, visible pass (the factory's own gates; for the baseline, the
same gates run on what it left), the hidden result, diff size and files against the reference,
dollars, minutes, gates passing on the first try, fix rounds and policy denials. The report shows
per-variant means over cases with the number of cases behind each, the **visible-versus-hidden
gap** (passes its own checks, fails the hidden tests: the reward-hacking signal), resolved rate by
case tag and by who wrote the reference, and differences against a baseline variant paired by
case, each marked `likely real`, `within noise` (under two standard errors) or `too few cases`.
Cursor variants run Grok 4.7; Cursor reports tokens rather than dollars, so its costs are
estimates (`usd_per_mtok`) and the report says so.

`run` options: `--cases ID ...`, `--repeat K`, `--jobs N` (parallel runs; one at a time by
default), `--max-usd` (stop starting runs past this spend), `--no-wait` (skip instead of waiting
out a usage limit), `--keep` (keep checkouts under `$ASF_HOME/evals/work`). Per-job budgets are
the variant's own; usage limits are shared with the scheduler (`asf/limits.py`). A case can set
`test_command`, `setup` commands, `deps_from` (where `execution.clone_dirs` such as `.venv` are
cloned from) and its own `config`; see `asf/evals/cases.py` for the format and
`tests/evals/example_case.yaml` for a synthetic example.

## Commands

| Command | What it does |
|---|---|
| `init [--name N] [--priority P]` | Setup agent: detects stacks, packages, test/lint/typecheck/build commands, dependency folders, risk-sensitive paths, trackers and the model CLIs installed here. Writes a pending config, commits it on `factory/setup` through a temporary worktree, and registers the project in the console. |
| `setup [check \| add PATH]` | Guided setup. Without arguments, a terminal wizard; `check` checks the machine (Python, git, a signed-in model backend; optional Node, Playwright, GitHub CLI); `add` adds a project with options (`--backend`, `--github-issues`, `--github-prs`, `--name`, `--priority`, `--print`). The console's Set up page (`asf ui --setup`) does the same. |
| `doctor` | Validates config, checks every command's executable, adapters and credentials. When healthy, registers the project. |
| `intake [--source S] [--id ID] [--rerun]` | Pulls work items from enabled intake adapters. Deduplicates by source and id: an item that already has a job, finished or not, is skipped, so a labelled issue is never rebuilt by accident. `--rerun` starts a new job for an item whose last job has ended. |
| `add --title ... [--accept ...]` | Queues a work item by hand. |
| `run [--job ID] [--all] [--stop-at STATE]` | Drives jobs through the state machine. Resumable after a crash. |
| `status [JOB]`, `events JOB [--jsonl]`, `report [--json]` | Job list, full audit trail of every agent tool call, metrics that count only shipped work. |
| `ui [--port N] [--open]` | The local web console for all projects (see above); with `--repo`, it opens that project. |
| `approve` / `reject JOB` | Human approval for tiers that require it. |
| `deploy JOB` | Run the deploy steps for a job that is ready to deploy (and every other one waiting). |
| `resume JOB [--extra-usd N]`, `retry JOB` | Continue a budget-paused job, or re-enter a failed one from a clean sandbox. |
| `revise JOB --note "..." [--notes-file F] [--extra-usd N]` | Send a delivered, unmerged job back with review notes. The implementer works on top of the delivered commit on the same branch, then the tester, gates and reviewers run again over the whole change, and one new commit is added. Each revision gets a fresh budget. Follow with `run --job JOB`. |
| `publish JOB` | Push a delivered job's branch and open a draft pull request (the report, plus `Fixes #N` for GitHub issues). Works whatever `delivery.provider` is; later revisions of the job update the same PR. |
| `reviews [--job JOB] [--dry-run]` | Review on GitHub: a comment starting with `/revise` on a factory PR sends the job back with notes (the rest of the comment plus every line comment on the diff since the last revision), pushes the new commit to the PR and replies with the result. Only the repo owner, members and collaborators are obeyed; other comments are ignored and logged. Run it by hand or on a schedule. |
| `repro [FILES] [--all] [--ref R]` | Run committed English reproductions in a real browser on the default branch (or `R`), in a clean worktree with the app started. Each must report its problem gone. Uses model time. |
| `e2e [--ref R]` | Run every package's e2e suite on the default branch with the app started. |
| `allow-repro-edit JOB` | Let a job's implementer change its committed reproduction test (the fix must change what it relies on). |
| `pause-all` / `resume-all` | Kill switch. A `PAUSED` file in the state directory works too. |
| `serve [--worker]` | Webhook gateway with signature verification, job API, optional worker loop. |
| `ci-workflow [--write]` | GitHub Actions workflow for runner mode. |
| `adapters` | Lists installed adapters, including third-party ones registered by entry point. |
| `eval import \| mine \| check \| run \| report \| list` | The eval harness (see above): draft cases from done jobs or history, sort their hidden tests, run variants, compare them. |

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
| Browser reproductions: app lifecycle, reproducer agent (`claude -p` + `@playwright/mcp` only), e2e suite | `asf/apprun.py`, `asf/repro.py`, `asf/demo.py` | Scripted tests for every path; live-tested on the console scroll bug (`tests/test_repro_real.py`, opt-in) |
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
| Eval harness: cases with hidden tests, variants as config overlays, interactive baseline, report with the visible-versus-hidden gap | `asf/evals/` | Tested with the scripted provider and a fake `claude`; smoke-run live on a real case |
| Models: native Claude Code and Cursor sessions for the writing roles, policy by pre-tool hook, `run_check`/`finish` over MCP, shell changes audited after each session | `adapters/models/native.py`, `claude_native.py`, `cursor_native.py`, `native_bridge.py` | Unit-tested with fake CLIs that call the real hook and MCP server; live-tested on a full job with each CLI |
| Not built yet | | See the plan in [docs/PARITY.md](docs/PARITY.md): hidden acceptance checks, earned autonomy, reviewer-burden metrics, per-project lessons, epic splitting |

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

The suite runs the full pipeline end to end with a scripted model, browser reproductions
included (`tests/test_repro.py`). `ASF_REAL_REPRO=1` adds one real run of the reproducer on the
console's scroll bug (`tests/test_repro_real.py`): Claude Code drives Chromium, reproduces the
bug, writes the Playwright test, and confirms the fix. It covers shadow
delivery, merge after approval, reviewer fix rounds, budget pause and resume, the kill
switch, prompt-injection containment, crash recovery, retry from a clean sandbox, and the
signed webhook gateway. No network calls are made and nothing is spent.
