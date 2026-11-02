# Agentic Software Factory design

System design for a software factory that a small company can adopt with its existing
toolchain and no platform team.

| File | What it holds |
|---|---|
| [DESIGN.md](DESIGN.md) | Full design: goals, adapter model, onboarding flow, core services, risk tiers, security, deployment modes, cost, failure modes, trade-offs |
| [factory.example.yaml](factory.example.yaml) | The single config file a company owns, in the schema the implementation accepts |
| [ventures/software-factory](../../ventures/software-factory/README.md) | The working implementation (`asf`) |

## The one-paragraph version

Work items arrive through intake adapters (GitHub Issues, Linear, Jira, Slack, Sentry,
PagerDuty) and are normalized into one schema. A durable orchestrator runs specialized
agents (implementer, tester, reviewer, security, CI fixer) inside sandboxes that by
default run on the company's own CI runners, so there is nothing to host. Agents work on
branches with single-branch tokens and never see production secrets. A policy engine
reads `factory.yaml` to decide per risk tier which gates run and whether a human must
approve. Deploy is merge-and-watch through the pipeline the company already trusts, with
a post-deploy gate on Sentry or Datadog. Trust is raised by editing the config file, so
every change to autonomy is a reviewable PR.

## Origin

Written 2026-11-02 as an interview-prep system design answer, then extended into a
product-shaped design after reviewing the existing market (Devin, Codex, Jules, Copilot
coding agent, Factory.ai), all of which stop at "opens a PR" and leave governance to the
customer.
