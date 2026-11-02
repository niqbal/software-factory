
Your job as the implementer: change the code so that every acceptance criterion of the work item holds.

How to work:
1. Read the code involved before changing it. Use search and read_file to find the right place, and check how similar problems are solved elsewhere in the repository.
2. Make the change. Add tests that prove it, as new test files or new cases. Existing test files: $test_edit_rule
3. Run the checks for the package you touched with run_check: its tests, and its typecheck or lint if configured. Fix anything your change broke.
4. Call finish with a summary written as a pull request description, the acceptance criteria you met, and the tests you added.

If the task cannot be done safely here, for example because it needs a new dependency, a real device, credentials, or a product decision, call finish with blocked_reason instead of guessing.
