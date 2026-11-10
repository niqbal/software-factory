# Claude Code or the factory?

Claude Code already writes code and tests it. This note answers three questions that follow:
why run the factory at all, whether calling the API costs more than a Claude subscription,
and whether Claude Code alone can give the same benefits.

## How the factory calls Claude

Every agent is a direct call to the Claude API through the Anthropic Python SDK
(`asf/adapters/models/anthropic_provider.py`), authenticated with `ANTHROPIC_API_KEY`
(`models.api_key_env` in `asf/config.py`). It is billed per token, not through a subscription.

The model provider is an adapter (`models.provider` in `factory.yaml`), so this is a choice
the code already allows changing, not a fixed part of the design.

## Cost

For one developer at the keyboard, Claude Code on a subscription is usually cheaper. The
subscription is a flat fee with usage limits, so while you stay under them, one more fix
costs nothing extra. The API charges for every token.

The per-job amounts are small, though. From the CycleSense run
([HOW-IT-WAS-BUILT.md, section 5](HOW-IT-WAS-BUILT.md#5-how-cost-is-measured)):

| | Model calls | Estimated cost |
|---|---|---|
| Privacy fix (triage, implementer, tester, reviewer, security) | 41 | $1.13 |
| All 9 jobs in the run | | $4.55 |

Prompt caching carries most of this. Nearly all input tokens are cache reads at a twentieth of
the input price.

An interactive Claude Code session doing the same fix reads and writes a similar number of
tokens. What changes is how it is paid for. Subscriptions are meant for one person's
interactive use and are rate limited. They are not sized for working a backlog unattended,
around the clock, or in CI.

## What the factory adds

The model is the same. The difference is the structure around it.

1. **Independent checking.** When Claude Code tests its own change, it grades its own work in
   the same context that produced it. In the factory the tester never sees the implementer's
   reasoning, and the reviewer and security agents read the diff cold.
2. **Rules enforced, not requested.** In Claude Code, `CLAUDE.md` is guidance the model can
   drift from. The factory's tool broker (`asf/broker.py`) is the only path from a model to
   the sandbox. It refuses writes to `permissions.protected_paths`, edits to existing tests
   above tier 0, deleting existing tests at any tier, commands outside the allowlist, and it
   redacts secrets from every tool output. The model's wording does not change any of this.
   In the CycleSense run, the stale Jest item stopped after six turns and $0.16 and named the
   two test files a human would have to allow, instead of rewriting tests until they passed.
3. **Deterministic gates.** Lint, typecheck, build and tests run for the packages the diff
   touches (`asf/gates.py`). A failure is re-run on the base commit, so failures that already
   existed are reported as pre-existing rather than blamed on the change, and real
   regressions cannot be argued away.
4. **Policy the team owns.** Risk tiers, required approvals and merge rights live in
   `factory.yaml`, changed only by a reviewed commit. The CycleSense privacy fix touches
   health data, so it is tier 3 and only a human can merge it.
5. **Unattended operation.** Intake from trackers, one worktree per job, per-job budgets
   checked before every model call, a kill switch, resume after a crash, and an audit trail
   of every agent tool call (`asf events JOB`).

## Getting the same benefits from Claude Code

Most of them are available in Claude Code, because they come from structure rather than
from the API:

| Factory feature | Closest Claude Code equivalent |
|---|---|
| Independent tester and reviewer | Subagents with their own context; `/code-review` |
| Broker rules (protected paths, test integrity) | `PreToolUse` hooks that block the call; permission allow and deny lists |
| Command allowlist | Permission rules for Bash |
| Unattended runs | Headless `claude -p`; the Claude Code GitHub Action |

What Claude Code does not provide, and you would build yourself, is what the factory already
has: a durable job state machine, per-job budgets, intake across several trackers, gates
compared against the base commit, and one policy file deciding who may merge what.

The two are not exclusive. A model provider adapter that runs agents through Claude Code
(`claude -p` or the Agent SDK) would keep the broker, gates and policy and change only how
the model is reached. Running automated work on a subscription that way is subject to its
rate limits and to Anthropic's terms for automated use, so check both before relying on it
for unattended runs.

## Is the factory meant to run as a GitHub workflow?

No. A GitHub workflow is one of three ways to run the same code, and it is not the one
CycleSense uses today.

| Mode | How it starts | Where agents work | What you get | Status |
|---|---|---|---|---|
| **Local CLI** | You run `asf intake` and `asf run` | A git worktree per job under `~/.asf/<repo>/sandboxes/` | A branch in your repository and a report; you review and merge | Used for CycleSense |
| **GitHub Actions runner** | Labelling an issue `factory`, or a manual dispatch | The Actions runner VM | A draft pull request with the report | Generated by `asf ci-workflow --write` and YAML-validated; not yet run live |
| **Server** | `asf serve --worker` on a host you keep running | Worktrees, or Docker with no network, on that host | Branches or pull requests, driven by tracker webhooks | Gateway tested with signed requests; not deployed |

What changes with the GitHub workflow:

- **No machine of yours has to be on.** GitHub starts the run when the issue is labelled.
- **It needs an `ANTHROPIC_API_KEY` repository secret**, and spend appears on that key's bill.
  Running agents through Claude Code on a subscription needs a CLI signed in on that machine,
  which a fresh runner does not have, so in Actions the factory uses the API.
- **State lasts only for the run.** The job database lives in the runner's temporary
  directory and is kept only as a downloadable artifact. Commands that work on a finished job
  later, such as `asf approve`, `asf revise` and `asf retry`, have nothing to act on in the
  next run. The review and merge happen on the pull request instead.
- **Delivery switches to pull requests** (`delivery.provider=github`), using the workflow's
  `GITHUB_TOKEN`.

The local CLI fits one developer who reviews every branch, and it keeps the job history for
revisions and retries. The workflow fits a team that wants labelled issues picked up without
anyone at a keyboard. Because the workflow has not been run live yet, try it on a low-risk
issue before relying on it.

## When to use which

- **Claude Code alone:** one developer, at the keyboard, reviewing each change as it happens.
  It is simpler and usually cheaper.
- **The factory:** a backlog to work through without someone watching, checks that run
  every time without anyone remembering to ask for them, and hard limits on what agents may
  touch. CycleSense handles health data, which is where those limits matter most.
