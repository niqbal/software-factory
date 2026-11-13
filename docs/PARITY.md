# Where the factory fits, why its code is worse today, and the plan

This page answers three questions.

1. What does the rest of the industry already do, and which parts of `asf` are worth keeping?
2. Why is code from an interactive Claude Code session better than code the factory writes
   through `claude -p` with the same model?
3. What do we build next, and what do we stop building?

The short version: **asking an agent for a pull request is now a commodity, and it is the
weakest part of `asf`. The parts few others do well are checking the agent's work
independently, a trust policy the team owns, and measuring whether work really ships. The
plan stops rebuilding the agent, runs Claude Code natively, and puts the effort into those three.**

Survey as of November 2026. Most sources are vendor pages and blogs, not audits; they are
listed at the end.

## Part 1: the landscape

| Category | Who | What they cover |
|---|---|---|
| Ticket in, PR out, from model and platform vendors | GitHub Copilot coding agent, OpenAI Codex cloud, Devin, Cursor Cloud Agents, Claude Code (`@claude` GitHub Action, Routines, background agents that open draft PRs, Managed Agents) | The agent loop, a hosted sandbox, a PR. A person reviews and merges. |
| Reviewers sold separately | Anthropic Code Review (multi-agent PR review, launched March 2026), `@codex review`, CodeRabbit | Findings on a PR. |
| Commercial factories | Factory.ai Droids (autonomy levels per environment, OpenTelemetry, audit logs), Warp Factories (a coordinating agent and stage agents, early access), Augment, Replit Agent | Intake, stages, sessions UI, enterprise controls, hosted. |
| Open-source factories | Mastra Factory (Apache 2.0: GitHub, Linear or Slack to investigate, plan, implement, review, with a web UI), OpenHands (issue resolver, Agent Canvas), AgenticKode | The same stages, self-hosted. Mastra Factory is the closest match to `asf`. |
| Running many agents at once | Conductor, Vibe Kanban, Sculptor (Terragon and Codegen shut down in 2026) | Parallel worktrees and a diff view on one machine. Tools, not factories. |
| Factories with no human code review | StrongDM (scenarios the coding agents never see, behavioral clones of Okta, Slack, Jira), OpenAI's harness engineering (about a million lines written by Codex) | Proof that the hard part is the environment and the checks, not the agent. |

**What is commodity, so we stop building it:** the agent loop and its tools, issue-to-PR
plumbing, hosted sandboxes, running agents in parallel, a PR review bot.

**What is still open, and where `asf` already leans:**

1. **Checks the author cannot game.** SpecBench (May 2026 preprint) found agents can pass
   every visible test while failing hidden ones, and the gap grows with code size. StrongDM's
   whole design exists to stop this. `asf` has an independent tester, gates compared against
   the base commit, browser proof before and after a fix, and locked reproduction tests. It
   does not yet have checks the implementer never sees.
2. **A trust policy the team owns.** Risk tiers, protected paths, test integrity and merge
   rights in a reviewed file in the repository, the same whichever agent does the work.
   Factory.ai's autonomy levels are proprietary and tied to its platform.
3. **Measuring whether work ships.** One buyer's guide notes that no public benchmark measures
   whether factories deliver, and says to measure reviewer burden per merged PR. `asf` already
   counts only shipped work, cost per shipped job and waste.

**Transparency on its own is not a gap.** Claude Code already emits OpenTelemetry events for
every tool decision and result, runs hooks before a tool call that can block it, and writes a
full transcript of every session. A factory on top of Claude Code can be fully audited without
rebuilding the agent.

## Part 2: why the factory's code is worse today

The factory does not run Claude Code. It runs a model behind a home-made tool interface,
without Claude Code's instructions, tools, project context, or the person who steers it.

### A. The harness

| # | Reason | Where | Effect |
|---|---|---|---|
| A1 | Claude Code's built-in tools are off (`--tools ""`). Tool calls are emulated as JSON forced by `--json-schema`. | `asf/adapters/models/claude_code.py:144` | Off the model's training distribution: fewer, clumsier tool calls; reasoning squeezed into a `text` field. |
| A2 | Claude Code's instructions are replaced (`--system-prompt`) by about 30 lines of role prompt. | `claude_code.py:145`, `asf/agents/prompts/` | The habits that make interactive output good (read first, verify, don't over-engineer) are missing. |
| A3 | Every tool round trip is a new `claude -p --resume` process in an empty temp folder, capped at 60 round trips. | `claude_code.py:87`, `asf/config.py:52` | Long tasks get cut off and nudged to finish. |
| A4 | The shell is read-only: pipes, `$`, `python -c`, starting the app are all refused. | `asf/broker.py:25-33` | The agent cannot probe behaviour or reproduce a bug by hand. |
| A5 | No subagents, todo list or plan mode. Search is `git grep` capped at 200 lines. | `--tools ""`, `broker.t_search` | Large tasks run in one linear conversation that fills with file dumps. |
| A6 | Cursor runs the same JSON emulation, and this repo routes the implementer to Cursor first. | `cursor_cli.py:8-10`, `factory.yaml:94-95` | Much "Claude" output is not Claude. |

### B. Context

| # | Reason | Where | Effect |
|---|---|---|---|
| B1 | Instruction files share 12,000 characters, 6,000 per file. This repo's 22,000-character README is mostly cut. | `asf/agents/context.py:39-45` | Conventions and architecture notes are missing. |
| B2 | No `CLAUDE.md` discovery: `--setting-sources ""` and a temp working folder. Nested and folder-level files never load. | `claude_code.py:146` | Folder-specific guidance never reaches the agent working there. |
| B3 | No memory across jobs. Review findings, `/revise` notes and rejections teach nothing. | | The same mistakes recur. |
| B4 | A ticket instead of a conversation. The only question the factory can ask is at triage. | `orchestrator._s_queued` | Plausible, wrong-shaped solutions that pass their own tests. |
| B5 | Fix rounds start a new conversation instead of resuming the implementer's session. | `roles.run_agent` | The second attempt re-explores and fixes the symptom named in the finding. |

### C. Checking its own work

| # | Reason | Where | Effect |
|---|---|---|---|
| C1 | Only configured commands run. The implementer cannot start the app or look at a page. | `broker.t_run_check` | UI work is coded blind and judged by tests the same job wrote. |
| C2 | One fix round by default. | `asf/config.py:78-79` | A second real defect goes straight to a human. |
| C3 | Sandboxes get a minimal environment and no generated files, so tests fail for reasons unrelated to the change. | `asf/util.py:100-110`, `setup_agent.detect` | Wasted rounds, or a "pre-existing failure" verdict that hides a real one. |

### D. Policy that trades quality for safety (deliberate; make each a conscious setting)

Existing tests frozen above tier 0 (`broker._check_write`); "as small as possible", judged by a
reviewer told to flag anything extra (`prompts/_common.md:7`); no new dependencies below tier 2;
diff-size caps.

## Part 3: the strategy

1. **The agent is a replaceable worker.** `asf` drives Claude Code natively by default, with
   Cursor and Codex as other workers. It never reimplements tools, sandboxes or review bots a
   vendor already ships; it calls them through adapters.
2. **`asf` owns what sits around the agent:** the policy file, checks the author cannot see,
   the evidence and audit trail, the numbers on what ships, and the owner's review page.
3. **Audit comes from the agent's own signals.** Claude Code hooks enforce the policy before
   a tool runs; its OpenTelemetry events and transcripts become `asf`'s audit trail.
4. **Autonomy is earned with data.** A tier is allowed to merge on its own only when the
   numbers for that tier say so, and the owner approves the change to `factory.yaml`.

## Part 4: the plan

### Phase 0: measure (built: `asf eval`)

Without numbers, "better" is an opinion.

- **Eval set:** 20 to 30 closed tickets from real projects, each with the merged solution as
  reference. Kept in the private runs archive, never in product repositories.
- **Scores per run:** gates pass on first try; reviewer blocking findings; owner revisions;
  **hidden-test pass rate** (the reference solution's tests run against the factory's change);
  **visible-versus-hidden gap** (the SpecBench measure of gaming); diff size against the
  reference; minutes and list-price cost.
- **Baselines:** the current harness, and interactive Claude Code given only the ticket text.
- `asf eval run --set NAME --config VARIANT`, so every later phase is an A/B on the same set.

What was built (`asf/evals/`, usage in the README's Evals section):

- **Cases** are YAML in `~/work/asf-runs/evals/sets/<set>/cases/`: repository, base commit,
  the ticket as written (title, body, acceptance criteria if any), reference commit. The hidden
  tests are the test files the reference adds or changes; `asf eval check` confirms they fail on
  the base commit and pass on the reference. The harness refuses an archive inside this
  repository or a product repository.
- **Mining:** `asf eval mine --repo PATH [--since DATE] [--limit N]` drafts cases from
  first-parent history (a merged PR is one unit) that touch code and tests, change under 400
  lines and reference an issue or have a descriptive message. Drafts are marked
  `review: needed`; a person rewrites the ticket as the requester would have and prunes.
- **Runs:** each case x variant x repeat gets its own clone holding only the base commit's
  history (no remote, no reference), a throwaway project in its own state folder, and one local
  work item, driven by `Factory.run_job` in shadow mode with local-branch delivery. Transient
  failures retry as the scheduler would; usage limits are waited out (or skipped with
  `--no-wait`); per-job budgets apply and `--max-usd` caps the run. `--jobs N` runs in parallel.
  Scoring writes the reference's test files over the factory's branch and runs each hidden test
  file on its own.
- **Variants** are `factory.yaml` overlays: `current`, `claude_code`, `opus-high`,
  `claude_code_native`, `cursor_native`, or a file in the archive's `variants/`. A variant whose
  provider this build lacks is reported and skipped. `interactive` runs `claude -p` with its
  default tools (Bash allowed, edits accepted) in the checkout on the ticket text alone, then
  the same deterministic gates decide its visible pass.
- **Report:** `asf eval report --set NAME` prints and writes (`reports/latest.{md,json}`)
  per-variant means over cases with case counts: resolved, hidden pass rate, visible pass, the
  visible-versus-hidden gap, gates first try, size against the reference, fix rounds, denials,
  dollars and minutes. Differences against the baseline are paired by case and marked
  `likely real`, `within noise` (under two standard errors) or `too few cases` (under three);
  fewer than 10 cases and no repeats are flagged.
- **First live run** (2026-11-13, run `smoke-20261113`): two small cases from this repository's
  own history, variant `claude_code`, run in parallel. Both were delivered in shadow mode with
  every hidden test passing and no gate failures ($5.28 list price, 6 and 8 minutes). Two cases
  prove only that the harness works end to end, not how good the factory is.
- Known limit: the hidden pass rate counts every test in a hidden file, including ones that
  already passed on base, so `resolved` (all hidden tests pass) is the measure to compare.
- Not yet scored: reviewer blocking findings are recorded per run, but owner revisions need a
  human in the loop and are left to Phase 4.

### Phase 1: run Claude Code natively (built: `models.provider: claude_code_native`)

The implementer, tester and CI fixer are real Claude Code sessions in the job's worktree. The
broker no longer executes their file and shell tools; it decides on them.

```
claude -p --output-format stream-json --verbose
  --append-system-prompt <role prompt + tool mapping>   # Claude Code's own instructions stay
  --tools Read,Edit,Write,Glob,Grep,Bash,Task,Skill
  --permission-mode dontAsk --permission-prompts none   # nothing prompts
  --settings <generated: PreToolUse hook -> asf/native_bridge.py hook>
  --setting-sources project                             # CLAUDE.md, .claude/rules, skills from the repo
  --strict-mcp-config --mcp-config <asf server: run_check, finish>
  --model M --effort E --max-turns 150 --max-budget-usd <job's remaining $> [--resume S]
  cwd = the job's worktree, env = the scrubbed sandbox environment
```

What was built (`asf/adapters/models/claude_native.py`, `asf/native_bridge.py`):
- **Policy by hook.** Each tool call goes to a token-protected loopback endpoint in the factory
  process, which applies the broker's own checks (`_rel`, `_check_read`, `_check_write`, the
  command deny-list), so protected paths, secret files, test integrity, the reproduction lock and
  dependency folders hold. `.claude/**` is protected too; reads outside the checkout are refused.
  Every decision is a `tool.call` event, as before, so the console and audit trail are unchanged.
- **The factory's own tools over MCP.** `run_check` and `finish` are an MCP server backed by the
  same broker, so checks run exactly as the gates will (e2e starts the app) and results land in
  the same fields. No `--json-schema`: finishing is a tool call, as in the other modes.
- **A real shell, audited.** Bash is a real shell; network tools, installs, git writes and
  commands naming secret files are refused. A shell can still write files the hook never sees,
  so after each session every changed file is checked against the role's rules and anything not
  allowed is put back to where it was before the session.
- **Budgets inside the session:** the job's remaining dollars (`--max-budget-usd`) and minutes
  (wall clock). Spend is recorded from Claude Code's own per-session cost.
- **Fix rounds resume the implementer's session** (`--resume`), so it keeps what it learned.
- Usage limits and API errors fail like the other providers, so the scheduler waits for the
  reset or a pool moves the role to another backend (`[backend:name]`).
- Reviewers and triage keep the structured path; Docker sandboxes fall back to it (a shell on
  this machine would run outside the container).
- **Cursor too** (`models.provider: cursor_native`). `cursor-agent -p` fires a `preToolUse` hook
  for every Read, Write, Delete, Grep, Shell and MCP call and honours its denials, so the same
  bridge, policy and audit apply. Differences: the hook and MCP files must live in the
  checkout's `.cursor/` (written per session, restored after, hidden from the agent); the role
  prompt goes in the first message (no way to append to Cursor's own); MCP calls time out after
  about a minute, so long checks run in the background behind `check_result`; no in-session
  budget or turn cap, only the wall clock. The shared parts are in `adapters/models/native.py`.

Still open in this phase: the instruction budget and model defaults (Opus at `high` for the
writing roles, two fix rounds) are left to `factory.yaml` until the Phase 0 evals say which wins;
`claude_code_native` is opt-in beside `claude_code` for the same reason. Once the evals favour
it, the emulated tool protocol and the broker's `t_*` executors are kept only for backends
without a native mode.

### Phase 2: checks the author cannot game (2 weeks; the main differentiator)

- **Hidden acceptance checks.** At triage (or from the owner), acceptance criteria become
  end-to-end checks stored in the state folder, outside the sandbox. The implementer and tester
  never see them. After the gates, they run: browser checks through the existing reproducer for
  UI, scripted checks for APIs and CLIs, and a model judge only where nothing deterministic fits.
- **Score, not just pass or fail:** the share of hidden checks that hold, reported next to the
  visible tests. A job whose visible tests pass but hidden checks fail goes back with the
  evidence, never with the hidden check itself.
- **Reproductions as a regression library.** Committed English reproductions and e2e tests
  already accumulate, and every job already reruns the ones whose `covers:` paths it touches.
  Their pass history becomes part of the numbers.
- **Fakes for outside services, only where a project needs them** (StrongDM's behavioral
  clones): a `fixtures:` entry in `factory.yaml` that starts a fake before checks run. Not
  built until a project asks.

### Phase 3: the policy you own, used by any agent (1 week)

- `factory.yaml` stays the single, vendor-neutral policy. `asf` generates each worker's native
  controls from it: Claude Code permissions and hooks first, other agents' equivalents where
  they exist.
- **Earned autonomy:** per tier, `asf report` tracks owner revisions, reverts and post-merge
  failures. When a tier has been clean for N shipped jobs, the console proposes raising
  `autonomy.max_merge_tier` as a ready-made diff for the owner to merge. It never raises it
  itself.

### Phase 4: numbers that say whether it works (1 week)

- **Reviewer burden:** owner minutes per merged PR (time from opening the review to the
  decision), and revisions per shipped job.
- **Quality after merge:** reverts and post-merge check failures, per tier and per backend.
- **Per backend and model:** cost per shipped job and the hidden-check score, so routing is
  chosen on evidence.
- A weekly report in the console and as Markdown.

### Phase 5: plug in vendor pieces instead of building them (as needed)

- Claude Code Review or `@codex review` as optional extra reviewers, with their findings in the
  same review step.
- Claude Code Routines, GitHub Actions or Managed Agents as places to run the factory, through
  the existing runner mode, instead of more sandbox infrastructure of our own.

### Phase 6: context and memory (later)

- **Lessons per project**, proposed by `asf` when review findings or revision notes repeat, and
  accepted by the owner with one click. Accepted lessons go into every prompt.
- **Plan approval for tier 3:** the implementer writes a plan first, and the owner approves it
  in the console before code is written.
- **Splitting epics** into dependent work items instead of parking them.

### Stopped or parked

- New sandbox providers (microVMs, hosted sandboxes): use the vendors' (Phase 5).
- A UI for running many agents in parallel: Conductor and Vibe Kanban do this.
- Best-of-N implementations: an experiment once Phase 0 numbers exist, not a roadmap item.
- More intake adapters than the projects in use need.

### Success criteria

| Metric | Today | Target |
|---|---|---|
| Hidden-test pass rate on the eval set | baseline (Phase 0) | at or above interactive Claude Code |
| Visible-versus-hidden gap | baseline | under 5 points |
| Gates pass on first try | baseline | interactive baseline + 10 points |
| Owner revisions per shipped job | baseline | under 0.25 |
| Owner minutes per merged PR | not measured | falling month over month |
| Reverts after merge | not measured | none from agent-merged tiers |

## Part 5: positioning

For a team of 5 to 50 that already pays for Claude Code or Cursor: **autonomy you can prove,
measured and controlled by the owner.** Agents from any vendor do the work; `asf` decides what
they may touch, checks the result with tests they never saw, shows the evidence, and raises
autonomy only when the numbers allow it.

The main risk is that a vendor ships the same governance and hidden-check layer. Keeping the
core small and every vendor behind an adapter keeps `asf` useful either way: if a vendor ships
a better piece, it becomes another adapter.

## Part 6: setup tooling (built)

- `scripts/install.sh`: from a fresh clone to a working `asf` on PATH with the machine checked.
  Options add the browser suite, a first project and the console.
- `asf setup`: checks the machine (Python, git, a signed-in model backend; optional Node,
  Playwright, GitHub CLI) and adds a project with optional settings, as a terminal wizard or
  with flags.
- The console's **Set up** page (`asf ui --setup`): the same checks and wizard in the browser,
  with a preview of the drafted `factory.yaml`, a project health check and a form for the first
  work item.

## Sources

- [Mastra: best software factory platforms (2026)](https://mastra.ai/articles/best-software-factory)
- [Unblocked: AI software factory buyer's guide](https://getunblocked.com/blog/what-is-an-ai-software-factory-buyers-guide/)
- [Hashnode: AI software factory, the hype and what ships](https://hashnode.com/blog/ai-software-factory)
- [StrongDM Software Factory](https://factory.strongdm.ai/) and [Simon Willison's write-up](https://simonwillison.net/2026/Feb/7/software-factory)
- [Stanford Law: Built by agents, tested by agents, trusted by whom?](https://law.stanford.edu/2026/02/08/built-by-agents-tested-by-agents-trusted-by-whom/)
- [OpenAI: Harness engineering](https://openai.com/index/harness-engineering/)
- [SpecBench: reward hacking in long-horizon coding agents](https://arxiv.org/abs/2605.21384)
- [Factory.ai enterprise docs](https://docs.factory.ai/enterprise.md)
- [OpenHands GitHub integration](https://docs.openhands.dev/enterprise/integrations/github)
- [Augment: open-source agent orchestrators](https://www.augmentcode.com/tools/open-source-agent-orchestrators)
- [TechCrunch: Anthropic launches Code Review](https://techcrunch.com/2026/03/09/anthropic-launches-code-review-tool-to-check-flood-of-ai-generated-code/)
- [AI Catchup: Claude Code Routines](https://aicatchup.com/news/claude-code-routines)
- [Claude Agent SDK observability](https://code.claude.com/docs/en/agent-sdk/observability)
- [Runlayer: hooks versus OTLP for Claude Code](https://docs.runlayer.com/claude-code-otlp-monitoring.md)
- [Microsoft: securing CI/CD with the Claude Code GitHub Action](https://www.microsoft.com/en-us/security/blog/2026/06/05/securing-ci-cd-in-agentic-world-claude-code-github-action-case/)
