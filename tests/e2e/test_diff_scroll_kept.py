"""The diff box keeps its scroll position across the console's periodic refresh.

The console reloads the open job every 4 s. It used to rebuild the detail pane's HTML each time,
which put the scrolled diff back at the top a few seconds after you scrolled it.
"""

import os

from playwright.sync_api import Page

URL = os.environ["ASF_APP_URL"].rstrip("/") + "/#p/demo/JOB-demo01"


def test_diff_scroll_survives_refresh(page: Page):
    page.goto(URL)
    page.get_by_role("button", name="Diff", exact=True).click()
    box = page.locator("#detail .diff")
    box.wait_for()
    assert box.evaluate("e => e.scrollHeight > e.clientHeight"), "demo diff should overflow its box"

    box.evaluate("e => { e.scrollTop = 600; }")
    page.evaluate("() => window.scrollTo(0, 300)")  # the page itself scrolls too
    before = box.evaluate("e => e.scrollTop")
    assert before > 0
    page_before = page.evaluate("() => window.scrollY")
    assert page_before > 0

    page.wait_for_timeout(9000)  # two refresh cycles

    assert page.locator("#detail .diff").evaluate("e => e.scrollTop") == before
    assert page.evaluate("() => window.scrollY") == page_before
