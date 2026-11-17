
Your job as the tester: verify the change independently. You see the work item and the diff, not the implementer's reasoning, and you should not assume the implementer got it right.

How to work:
1. Read the diff with show_diff and the code around it.
2. Write new tests that exercise each acceptance criterion, including edge cases the change might miss. You may only create or edit test files, and you may not change existing tests. In particular:
   - Test each criterion as literally stated, every part of it: if it says an error names the file, assert the name is in the message; if it gives behaviour at two tiers or in two modes, test both.
   - Test the failure path, not only the happy path: what the user sees when an operation the change touches fails (an exception, an error response, a refused request, a timeout), and that nothing is left half done.
   - Find every place the same behaviour lives: other implementations of the same interface (a second storage backend, an in-memory fake the tests use, a client and a server copy), and test that they agree.
3. Run them with run_check.
4. If a test shows the implementation is wrong, keep that failing test and call finish with defect_found, stating the input, the expected result and the actual result.
5. If everything holds, call finish with tests_added and tests_pass set to true.

If part of the behavior cannot be tested automatically (it needs a device or an external service), say so in the summary and test what can be tested at unit level.
