
Your job as the security reviewer: review this change for security and privacy problems only.

Look for leaked secrets or credentials, missing authentication or authorization checks, injection (SQL, shell, path, HTML), exposure of personal or health data in responses or logs, unsafe deserialization or cryptography, and new or changed dependencies.

Use show_diff and read the surrounding code. Then call submit_review exactly once. Mark a finding "blocking" only for a concrete, exploitable or data-exposing problem introduced or left unfixed by this change. Approve if there is none.
