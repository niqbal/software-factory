import os
from playwright.sync_api import Page, expect

BASE = os.environ["ASF_APP_URL"].rstrip("/")


def test_diff_box_keeps_scroll_on_refresh(page: Page) -> None:
    page.goto(BASE + "/#p/demo/JOB-demo01")
    diff_tab = page.get_by_role("button", name="Diff", exact=True)
    expect(diff_tab).to_be_visible()
    diff_tab.click()
    diff = page.locator(".diff")
    expect(diff.first).to_be_visible()
    before = page.evaluate(
        "() => { const d = document.querySelector('.diff'); d.scrollTop = 800; return d.scrollTop; }"
    )
    assert before == 800, f"diff_scroll_top_before was {before}, expected 800"
    page.wait_for_timeout(6000)
    after = page.evaluate("() => document.querySelector('.diff').scrollTop")
    assert after > 700, f"diff_scroll_top_after was {after}, expected about 800"
