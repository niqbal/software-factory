"""A self-contained console with demo data, for browser reproductions and the e2e suite.

    python -m asf.demo --port 8790

Builds a throwaway project in a temporary folder (a git repo, a factory.db) holding one job,
JOB-demo01, delivered for review with a long diff, and serves the console for it at
/#p/demo/JOB-demo01. Nothing under ~/.asf is read or written, and no model is ever called:
the console only reads the store. This is the `app.start` command in the factory's own
factory.yaml.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import signal
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

KEY = "demo"
JOB = "JOB-demo01"
GIT_ENV = {"GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@asf.local",
           "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@asf.local"}
CONFIG = """version: 1
project: {name: demo, default_branch: main}
packages:
- name: demo
  root: .
  commands: {test: "true"}
intake: {}
notifications: {providers: []}
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


def build(home: Path) -> tuple[Path, Path]:
    """Create the demo repo and state dir under `home`; return (repo, state_dir)."""
    from .models import WorkItem
    from .store import Store

    repo, state_dir = home / "repo", home / "state"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "factory.yaml").write_text(CONFIG)
    (repo / "app.py").write_text("".join(f"def handler_{i}(request):\n    return {i}\n\n\n" for i in range(150)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "factory/demo-change")
    # A long change, so the Diff tab has a box to scroll through.
    (repo / "app.py").write_text("".join(f"def handler_{i}(request):\n    return {i} * 2  # doubled\n\n\n"
                                         for i in range(150)))
    _git(repo, "commit", "-qam", "Double every handler")
    _git(repo, "checkout", "-q", "main")

    store = Store(state_dir / "factory.db")
    item, _ = store.add_work_item(WorkItem("local", "demo-1", "Double every handler", kind="task",
                                           body="A demo job with a long diff."))
    data = {"branch": "factory/demo-change", "started": time.time(), "delivered_at": time.time(),
            "implementer": "Doubled every handler.", "acceptance": ["every handler returns double"]}
    now = time.time()
    store.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                     (JOB, item.id, "awaiting_review", 1, json.dumps(data), now, now))
    store.event(JOB, "job.created", "factory", {"work_item": item.id, "title": item.title})
    return repo, state_dir


def serve(port: int, home: Path | None = None) -> None:
    from . import config as config_mod
    from .orchestrator import Factory
    from .projects import Project
    from .store import Store
    from .ui import Console, Portfolio, make_handler

    own = home is None
    home = Path(home or tempfile.mkdtemp(prefix="asf-demo-"))
    repo, state_dir = build(home)
    cfg = config_mod.load(repo / "factory.yaml")
    f = Factory(repo, cfg, state_dir, Store(state_dir / "factory.db"), secrets=lambda n: None)
    portfolio = Portfolio()
    console = Console(f)
    console.token = portfolio.token
    console.project = Project(key=KEY, state_dir=state_dir, repo=repo, name="demo")
    portfolio.consoles[KEY] = console
    portfolio.projects = lambda: [console.project]  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(portfolio))
    # The factory stops the app with SIGTERM: clean up the demo folder then too.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f"demo console: http://127.0.0.1:{server.server_address[1]}/#p/{KEY}/{JOB}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if own:
            shutil.rmtree(home, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8790")))
    ap.add_argument("--home", help="folder for the demo data (default: a temporary folder, removed on exit)")
    args = ap.parse_args()
    serve(args.port, Path(args.home) if args.home else None)


if __name__ == "__main__":
    main()

