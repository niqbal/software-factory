"""The e2e suite: pytest-playwright tests run against the app the factory starts.

The factory sets ASF_APP_URL when it runs this suite (the e2e gate, post-merge checks,
`asf e2e`). Without it the tests are not collected, so a plain `pytest` stays fast and needs
no browser. Run it by hand with `asf e2e`, or start the app yourself and set ASF_APP_URL.
"""

import os

collect_ignore_glob = [] if os.environ.get("ASF_APP_URL") else ["test_*.py"]
