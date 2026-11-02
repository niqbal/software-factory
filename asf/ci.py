"""Generate the GitHub Actions workflow for runner mode."""

from __future__ import annotations

from importlib.resources import files
from string import Template

from .config import Config


def github_actions_workflow(cfg: Config, install: str | None = None) -> str:
    steps: list[str] = []
    stacks = set(cfg.get("project.stacks", []))
    roots = sorted({p.root for p in cfg.packages})
    if "node" in stacks:
        steps += ["      - uses: actions/setup-node@v4", "        with:", "          node-version: 22"]
        for root in roots:
            steps += [f"      - name: Install dependencies ({root})", f"        working-directory: {root}",
                      "        run: npm ci --no-audit --no-fund"]
    if "python" in stacks:
        steps += ["      - name: Install Python dependencies",
                  "        run: pip install -r requirements.txt || pip install -e '.[dev]'"]
    if "go" in stacks:
        steps += ["      - uses: actions/setup-go@v5", "        with:", "          go-version: stable"]
    tier_minutes = max(cfg.budget_for(t).get("minutes", 90) for t in range(4))
    template = Template((files("asf") / "templates" / "github-actions.yml").read_text())
    return template.safe_substitute(
        label=cfg.get("intake.github_issues.label", "factory"),
        timeout=tier_minutes + 15,
        setup_steps="\n".join(steps),
        install=install or cfg.get("ci.install", "pip install asf"),
    )
