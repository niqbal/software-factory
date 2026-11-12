from asf.adapters.base import AdapterContext
from asf.adapters.sandbox.local_worktree import LocalWorktreeProvider
from asf.gates import GateRunner
from conftest import PY, invoice_config, load_cfg, make_repo


def make_sb(repo, state, cfg, job="JOB-gc"):
    ctx = AdapterContext(repo=repo, state_dir=state, config=cfg)
    prov = LocalWorktreeProvider(cfg.get("execution"), ctx)
    return prov, prov.create(job, "main", f"factory/{job}")


def two_package_config() -> str:
    return invoice_config(**{
        "packages": [
            {
                "name": "invoice",
                "root": ".",
                "triggers": ["invoice/**", "tests/**"],
                "commands": {"test": f"{PY} -m pytest -q -p no:cacheprovider {{args}}"},
            },
            {
                "name": "scripts",
                "root": ".",
                "triggers": ["scripts/**"],
                "commands": {},
            },
        ]
    })


def test_test_gate_fails_when_diff_has_covered_and_uncovered_code_file(tmp_path, state):
    files = {
        "invoice/__init__.py": "",
        "invoice/totals.py": "def total_cents(items):\n    return sum(price * qty for price, qty in items)\n",
        "tests/test_totals.py": "from invoice.totals import total_cents\n\n\ndef test_total():\n    assert total_cents([(100, 2)]) == 200\n",
        "scripts/tool.py": "def run():\n    return 1\n",
        "README.md": "# demo\n",
    }
    repo = make_repo(tmp_path / "mixed", files)
    cfg = load_cfg(two_package_config())
    _, sb = make_sb(repo, state, cfg)

    (sb.root / "invoice" / "totals.py").write_text("def total_cents(items):\n    return sum(price * qty for price, qty in items) + 1\n")
    (sb.root / "scripts" / "tool.py").write_text("def run():\n    return 2\n")

    res = GateRunner(cfg, sb, 1, state).run(["test"])[0]
    assert res.status == "fail"
    assert "scripts/tool.py" in res.detail


def test_test_gate_warns_at_tier_zero_for_uncovered_code(tmp_path, state):
    files = {
        "invoice/__init__.py": "",
        "invoice/totals.py": "def total_cents(items):\n    return sum(price * qty for price, qty in items)\n",
        "tests/test_totals.py": "from invoice.totals import total_cents\n\n\ndef test_total():\n    assert total_cents([(100, 2)]) == 200\n",
        "scripts/tool.py": "def run():\n    return 1\n",
    }
    repo = make_repo(tmp_path / "tier0", files)
    cfg = load_cfg(two_package_config())
    _, sb = make_sb(repo, state, cfg)

    (sb.root / "invoice" / "totals.py").write_text("def total_cents(items):\n    return sum(price * qty for price, qty in items) + 1\n")
    (sb.root / "scripts" / "tool.py").write_text("def run():\n    return 2\n")

    res = GateRunner(cfg, sb, 0, state).run(["test"])[0]
    assert res.status == "pass"
    assert "warning:" in res.detail
    assert "scripts/tool.py" in res.detail


def test_test_gate_unaffected_when_all_changed_code_is_covered(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    _, sb = make_sb(invoice_repo, state, cfg)

    (sb.root / "invoice" / "totals.py").write_text("def total_cents(items):\n    return sum(price * qty for price, qty in items) + 1\n")
    (sb.root / "tests" / "test_totals.py").write_text(
        "from invoice.totals import total_cents\n\n\ndef test_total():\n    assert total_cents([(100, 2), (50, 1)]) == 251\n"
    )

    results = [r for r in GateRunner(cfg, sb, 1, state).run(["test"]) if r.name == "test"]
    assert len(results) == 1
    assert results[0].status == "pass"
    assert "warning:" not in results[0].detail


def test_test_gate_unaffected_for_docs_and_config_only(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    _, sb = make_sb(invoice_repo, state, cfg)

    (sb.root / "README.md").write_text("# invoice\n\nupdated docs\n")
    (sb.root / "pyproject.toml").write_text("[project]\nname = 'invoice'\n")

    res = GateRunner(cfg, sb, 1, state).run(["test"])[0]
    assert res.status == "pass"
    assert "warning:" not in res.detail


def test_test_gate_fails_for_uncovered_code_only(tmp_path, state):
    files = {
        "invoice/__init__.py": "",
        "invoice/totals.py": "def total_cents(items):\n    return sum(price * qty for price, qty in items)\n",
        "tests/test_totals.py": "from invoice.totals import total_cents\n\n\ndef test_total():\n    assert total_cents([(100, 2)]) == 200\n",
        "scripts/tool.py": "def run():\n    return 1\n",
    }
    repo = make_repo(tmp_path / "uncovered", files)
    cfg = load_cfg(two_package_config())
    _, sb = make_sb(repo, state, cfg)

    (sb.root / "scripts" / "tool.py").write_text("def run():\n    return 2\n")

    res = GateRunner(cfg, sb, 1, state).run(["test"])[0]
    assert res.status == "fail"
    assert "scripts/tool.py" in res.detail
