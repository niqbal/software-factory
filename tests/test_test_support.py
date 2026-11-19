"""Existing tests stay frozen above tier 0; test support files (fakes, helpers, fixtures) may change."""

import pytest

from asf import policy
from conftest import invoice_config, load_cfg

FAKE = "export class MemStorage {\n  getShared() { return []; }\n}\n"
JEST = "import { MemStorage } from './helpers/memStorage';\n\ntest('shares', () => {\n  expect(1).toBe(1);\n});\n"


@pytest.fixture
def cfg():
    return load_cfg(invoice_config())


@pytest.mark.parametrize("path,text,frozen", [
    ("server/__tests__/helpers/memStorage.ts", FAKE, False),          # a fake: may change with the code
    ("server/__tests__/share.test.ts", JEST, True),                    # a test file by name
    ("server/__tests__/shareCases.ts", JEST, True),                    # a test file by content
    ("tests/conftest.py", "import pytest\n\n@pytest.fixture\ndef db():\n    return {}\n", False),
    ("tests/helpers.py", "def make():\n    return 1\n", False),
    ("tests/checks.py", "class TestTotals:\n    def test_a(self):\n        assert True\n", True),
    ("pkg/thing_test.go", "package pkg\n", True),
    ("invoice/totals.py", "def f():\n    return 1\n", False),               # not a test path at all
])
def test_frozen_test(cfg, path, text, frozen):
    assert policy.frozen_test(cfg, 2, path, text) is frozen


def test_nothing_frozen_at_tier_zero_or_for_new_files(cfg):
    assert not policy.frozen_test(cfg, 0, "tests/test_a.py", "def test_a():\n    assert 1\n")
    assert not policy.frozen_test(cfg, 2, "tests/test_new.py", None)


def test_support_edits_can_be_switched_off():
    cfg = load_cfg(invoice_config(permissions={"implementer": {"may_edit_test_support": False}}))
    assert policy.frozen_test(cfg, 2, "tests/helpers.py", "def make():\n    return 1\n")
