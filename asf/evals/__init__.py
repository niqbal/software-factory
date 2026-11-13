"""Evals: measure the factory's code against hidden tests, per variant, on the same task set.

One case is a repository, a base commit, the ticket as the requester wrote it, and the merged
reference solution. The tests the reference added or changed are the case's hidden tests: the
factory never sees them. A variant is a named `factory.yaml` overlay (or the `interactive`
baseline: plain `claude -p` on the ticket text). `asf eval run` runs each case through the real
pipeline in shadow mode and scores it; `asf eval report` compares variants.

Cases, results and reports live in the private runs archive (default `~/work/asf-runs/evals`),
never in this repository or a product repository. See docs/PARITY.md, Phase 0.

    cases.py     case format, the archive layout, the guard that keeps cases out of repos
    variants.py  built-in and archive variants, overlays, availability
    workspace.py mirrors, clean checkouts at the base commit, the eval project's config
    score.py     hidden tests, diff size against the reference, numbers from the job
    runner.py    one case x variant x repeat through the factory or the interactive baseline
    mine.py      draft cases from merged history
    report.py    per-variant tables, the visible-versus-hidden gap, noise flags
"""
