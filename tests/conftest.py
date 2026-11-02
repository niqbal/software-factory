from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from asf import config as config_mod
from asf.store import Store

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin"}


def sh(cwd: Path, *args: str) -> str:
    import os
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    sh(root, "git", "init", "-q", "-b", "main")
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content).lstrip())
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "init")
    return root


PY = sys.executable

INVOICE_FILES = {
    "invoice/__init__.py": "",
    "invoice/totals.py": """
        def total_cents(items):
            \"\"\"Sum line items given as (unit_price_cents, quantity).\"\"\"
            return sum(price * qty for price, qty in items)


        def apply_discount(cents, percent):
            # BUG: truncates instead of rounding half up
            return int(cents * (100 - percent) / 100)
        """,
    "invoice/auth.py": "def check(token):\n    return token == 'x'\n",
    "tests/__init__.py": "",
    "tests/test_totals.py": """
        from invoice.totals import total_cents


        def test_total():
            assert total_cents([(100, 2), (50, 1)]) == 250
        """,
    "README.md": "# invoice\n",
    "CLAUDE.md": "Use plain functions. Money is integer cents.\n",
}


def invoice_config(**overrides) -> str:
    base = f"""
    version: 1
    project: {{name: invoice, default_branch: main}}
    packages:
      - name: invoice
        root: .
        triggers: ["**"]
        commands:
          test: "{PY} -m pytest -q -p no:cacheprovider {{args}}"
    intake:
      local: {{enabled: true}}
    tiers:
      rules:
        - {{paths: ["docs/**", "**/*.md"], tier: 0}}
        - {{paths: ["invoice/auth.py"], tier: 3}}
      classifier_default: 2
    notifications: {{providers: []}}
    """
    import yaml
    raw = yaml.safe_load(textwrap.dedent(base))
    for k, v in overrides.items():
        node = raw
        parts = k.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = v
    return yaml.safe_dump(raw)


@pytest.fixture
def invoice_repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "invoice", INVOICE_FILES)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def store(state: Path) -> Store:
    return Store(state / "factory.db")


def load_cfg(text: str) -> config_mod.Config:
    return config_mod.load(text=text)
