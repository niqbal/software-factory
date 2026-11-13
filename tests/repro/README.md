# Browser reproductions

Each `<slug>.md` here is an English reproduction: front matter (`item`, `kind`, `covers`, `path`,
`test`) and numbered steps, the expected result and the values to measure. The factory's
reproducer agent runs it in a real browser against the app (`app:` in factory.yaml), and wrote
the matching Playwright test in `tests/e2e/`.

They run when a change touches a path in `covers:`, and with `asf repro --all`. See the README
at the repository root, "Browser reproductions".
