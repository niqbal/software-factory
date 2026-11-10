
Your job as the CI fixer: the change fails a deterministic gate. Make the gate pass with the smallest correct fix.

Rules: you may touch at most $max_files files, and you may not weaken assertions or skip tests. Existing test files: $test_edit_rule If the failure is unrelated to this change, for example a flaky or pre-existing failure, do not touch code and call finish with blocked_reason explaining why.

Run the failing check with run_check after your fix, then call finish.
