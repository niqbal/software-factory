import pytest

from asf import policy
from asf.config import ConfigError, load
from asf.models import WorkItem
from asf.util import glob_match, redact
from conftest import invoice_config, load_cfg


def test_glob_match():
    assert glob_match("src/a/b.test.ts", "**/*.test.*")
    assert glob_match("a.test.ts", "**/*.test.*")
    assert glob_match("server/__tests__/x.ts", "**/__tests__/**")
    assert glob_match("mobile/eas.json", "eas.json")
    assert not glob_match("src/app.ts", "docs/**")
    assert glob_match("docs/a/b.md", "docs/**")


def test_defaults_are_safe():
    cfg = load_cfg(invoice_config())
    assert cfg.get("autonomy.shadow") is True
    assert cfg.get("autonomy.max_merge_tier") == -1
    assert "factory.yaml" in cfg.get("permissions.protected_paths")


def test_validation_errors():
    with pytest.raises(ConfigError):
        load(text="version: 2\n")
    with pytest.raises(ConfigError):
        load(text="version: 1\ngates: {tier_1: {merge: yolo}}\n")


def test_tiers_from_paths_and_labels():
    cfg = load_cfg(invoice_config())
    assert policy.tier_for_paths(cfg, ["README.md"]) == 0
    assert policy.tier_for_paths(cfg, ["invoice/totals.py"]) == 2
    assert policy.tier_for_paths(cfg, ["invoice/totals.py", "invoice/auth.py"]) == 3
    item = WorkItem("local", "1", "x", labels=["tier:1"])
    assert policy.pre_tier(cfg, item, 3) == 1
    assert policy.final_tier(cfg, item, 1, ["invoice/auth.py"]) == 1  # human label wins
    plain = WorkItem("local", "2", "y")
    assert policy.final_tier(cfg, plain, 1, ["invoice/auth.py"]) == 3  # diff raises the tier


def test_merge_decisions():
    shadow = policy.decide(load_cfg(invoice_config()), 1)
    assert not shadow.may_agent_merge and "shadow" in shadow.reason
    live = load_cfg(invoice_config(**{"autonomy.shadow": False, "autonomy.max_merge_tier": 2}))
    assert policy.decide(live, 1).may_agent_merge
    assert policy.decide(live, 2).approvals_required == 1
    assert not policy.decide(live, 3).may_agent_merge  # human-only tier


def test_redaction():
    text = "key sk-ant-api03-abcdefghijklmnop and ghp_abcdefghijklmnopqrstuvwx"
    out = redact(text)
    assert "sk-ant" not in out and "ghp_" not in out


def test_manual_verification_blocks_agent_merge():
    live = load_cfg(invoice_config(**{"autonomy.shadow": False, "autonomy.max_merge_tier": 3}))
    d = policy.decide(live, 1, manual_checks=["check on a real iPhone"])
    assert not d.may_agent_merge and "manual verification" in d.reason


def test_placeholder_clarifications_are_not_questions():
    from asf.orchestrator import _real_question
    for q in ['""', "", "  ", "None", "N/A", "''", None]:
        assert not _real_question(q), q
    assert _real_question("Which fault-hook API should memstore expose?")
