---
item: github_issues:6
kind: bug
covers:
- asf/ui_static/**
path: /#p/demo/JOB-demo01
test: tests/e2e/test_the_diff_box_jumps_to_the_top_on_the_4_s.py
---
# The diff box jumps to the top on the 4 s refresh

Reproduces the bug in a real browser. Run by the factory's reproducer agent; `tests/e2e/test_the_diff_box_jumps_to_the_top_on_the_4_s.py` checks the same thing deterministically.

## Steps

1. Open the start page. Wait until the job "Double every handler" is shown with its tabs (Overview, Diff, Report, Timeline).
2. Click the "Diff" tab.
3. Scroll the diff box (the scrollable box with class `diff` that holds the patch lines) down by setting its scrollTop to 800.
4. Read its scrollTop: it is 800.
5. Wait 6 seconds without touching the page. The console refreshes the open job every 4 seconds.
6. Read the diff box's scrollTop again.

## Expected

The diff box stays where it was scrolled to: scrollTop is still about 800 after the refresh.

## Measure

- diff_scroll_top_before: the diff box's scrollTop right after scrolling (expected: 800)
- diff_scroll_top_after: the diff box's scrollTop after the 6 second wait (expected: about 800; bug: 0)
