# Add a project to the factory

The same steps work for a brand-new repository and for one with years of history. A project
is a git repository with a `factory.yaml`, plus a state folder under `~/.asf` that the
factory creates. One factory, one console and one scheduler serve every project.

Projects already set up this way: `~/work/workflow-engine`, `~/work/gamedev` and this
repository itself. Their `factory.yaml` files are worked examples.

The fastest path is the guided setup, which does steps 0 to 5 for you:

```bash
scripts/install.sh --project ~/work/myapp --ui   # install, add the project, open the Set up page
```

or `asf setup` in a terminal, or **Set up** in the console. The steps below are what it does,
for when you want to do them by hand.

## 0. Before the first project (once per machine)

```bash
cd ~/work/software-factory && python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
ln -sf ~/work/software-factory/.venv/bin/asf ~/.local/bin/asf   # puts asf on PATH
claude --version          # Claude Code CLI, signed in (subscription; no API key needed)
cursor-agent --version    # Cursor CLI, signed in (optional; adds a second backend)
```

Either CLI is enough. With both, projects use a pool: Cursor implements, Claude reviews,
and each takes over when the other hits its usage limit.

## 1. Get the repository ready

**New project:** create the repository and give agents something to build on.

```bash
mkdir ~/work/myapp && cd ~/work/myapp && git init -b main
# Add a README (what it is), a manifest (package.json, pyproject.toml, go.mod, ...),
# one passing test, and commit. The factory needs a test command that passes on main.
git add -A && git commit -m "Skeleton"
```

**Existing project:** make sure it is a git repository with the default branch checked out
somewhere, and that its tests pass on that branch today. A red main makes every job fail its
gates. Commit or stash nothing for the factory's sake: it works in its own git worktrees and
never touches your checkout.

In both cases, an instructions file at the root (`AGENTS.md` or `CLAUDE.md`) is the most
useful thing you can add: agents read it before every job.

## 2. Draft `factory.yaml`

```bash
asf --repo ~/work/myapp init --name "My app" --priority 2
```

`init` reads the repository and:

- detects stacks and packages (one per `package.json`, `pyproject.toml`, `go.mod`, ...), with
  their test, lint, typecheck and build commands;
- marks risky paths (auth, payments, migrations, infra, deploy files, `.github`) as tier 3;
- drafts `models:` from the CLIs on this machine (pool if both are installed) and, for CLIs,
  budgets where only time limits matter;
- registers the project, so it appears in the console right away;
- writes the draft to `~/.asf/<project>/factory.yaml` and commits it on a `factory/setup`
  branch through a temporary worktree.

`asf --repo . init --print` shows the draft without writing anything.

## 3. Review and edit the draft

Detection is a starting point. Go through this list; the examples show each item.

| Check | Why | Example |
|---|---|---|
| Every package has a `test` command that passes on main | Gates run these on every job | gamedev: two games had tests but no `package.json`, so their commands were added by hand |
| Commands use absolute paths for toolchains not on PATH | Sandboxes get a scrubbed environment | workflow-engine: `~/sdk/go1.27.4/bin/go` |
| Tier rules match where the risk really is | Tier 3 is owner-only and shown as its own diff | software-factory: broker, policy, gates, providers |
| `permissions.protected_paths` lists files agents must never edit | Contract tests, secrets, release scripts | gamedev: `.claude/**`, the publish scripts |
| `context.instruction_files` names the agents' instructions | Read before every job | `AGENTS.md`, `CLAUDE.md` |
| `limits.max_changed_lines` fits the kind of work | Content and tests make long diffs | gamedev: 2,500 at tier 2 |
| `observe.post_merge` runs the full suite | A job is done only when main still passes after the merge | `make ci`, every game's tests |
| `deploy.steps` say what "live" means, if anything | A job is done only after its deploy passes | gamedev: publish the site, owner approval |
| `autonomy` stays `shadow: true, max_merge_tier: -1` at first | Nothing merges until you merge it | Raise it later, per tier, once the work looks right |

Then make it official, either way:

```bash
cd ~/work/myapp && git merge factory/setup        # take init's commit (then edit and commit)
# or: copy ~/.asf/<project>/factory.yaml into the repo, edit it, and commit it on main
```

The copy in the repository wins over the pending one. Agents can never edit `factory.yaml`;
every change to it is yours.

## 4. Check it

```bash
asf --repo ~/work/myapp doctor
```

`doctor` checks the git repository, the default branch, every command's executable, the
adapters and each model backend, and prints which backend each role uses. When it says
`healthy`, it also registers the project (useful when you wrote `factory.yaml` by hand and
skipped `init`).

## 5. Give it work

A work item is a Markdown file with YAML front matter. Put it in the project's inbox
(`~/.asf/<project>/inbox/`, or the folder set in `intake.local.path`):

```markdown
---
title: Add a pause menu to Golconda
kind: feature
acceptance:
  - Esc opens a pause menu with Resume, Sound and Quit to title
  - the game clock stops while it is open
---
Players lose progress when they step away. Follow golconda/CLAUDE.md.
```

Or queue one directly: `asf --repo ~/work/myapp add --title "..." --accept "..."`. Trackers
(GitHub issues with a `factory` label, Linear, Jira, beads) are switched on under `intake:`.
Write acceptance criteria a test can check; vague items come back as questions.

## 6. Run it

Open the console and start the scheduler there:

```bash
asf ui --open        # http://127.0.0.1:8787, every project
```

The scheduler picks up new items every minute. Its mode decides how projects share the
agents: **priority** finishes the lowest priority number first, **combined** takes turns,
**separate** runs each project independently. Change a project's priority on its card.

## 7. Review, merge and deploy

Delivered jobs land in the console inbox as a branch with a report. Tier-3 changes show the
owner-only part as its own diff, with the agent's `OWNER REVIEW:` notes. Merge, send back
with notes, or reject from the job page. After a merge the factory runs `observe.post_merge`
on main. If a deploy step matches the change, the job comes back to the inbox as **ready to
deploy** with the step's preview; click Deploy (or `asf deploy JOB`). Only then is the job
**done**. See the main README for job states and the `deploy:` format.

Projects without deploy steps (a library, the workflow engine) are done once main passes.

## Project notes

- **gamedev**: changes are judged by playing them, so nothing merges by itself. Playtest
  the branch (or ask the playtester agent in a Claude session) before merging. A merged change
  to a game's `src/` waits as ready to deploy with the release check's report; Deploy runs the
  release check, `tools/update-published-site.sh` and `tools/publish-site.sh` (commit and
  push `published_site/`, which Amplify serves) from a clean copy of main. Skyracer (Unity)
  has no command-line tests, its build is not in git, and the published copy is kept.
- **software-factory**: the factory works on itself. Its trust boundary is tier 3 and the
  tests that prove it are protected. Sandboxes import their own copy of `asf`, so a job never
  changes the running factory; a merge does, so restart `asf ui` after merging.

## Remove or pause a project

Archive it on its console card (or `archived: true` in `~/.asf/projects.yaml`): the
scheduler skips it and its history stays. `asf --repo ~/work/myapp pause-all` stops one
project's agents without archiving it.
