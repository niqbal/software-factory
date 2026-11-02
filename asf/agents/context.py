"""Repository context for agent system prompts: conventions, layout, commands.

The system prompt is identical across a job's agent runs, so it is cached.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..config import Config
from ..util import run

SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv", "venv", "__pycache__", ".next",
             "coverage", "android", "ios", ".expo", "target", "vendor"}


def layout(root: Path, max_lines: int = 120) -> str:
    res = run(["git", "-C", str(root), "ls-files"], shell=False)
    files = [f for f in res.stdout.splitlines() if f and not any(p in SKIP_DIRS for p in f.split("/")[:-1])]
    dirs: Counter[str] = Counter()
    for f in files:
        parts = f.split("/")
        for depth in (1, 2, 3):
            if len(parts) > depth:
                dirs["/".join(parts[:depth]) + "/"] += 1
    top_files = sorted(f for f in files if "/" not in f)
    lines = [f"{len(files)} tracked files. Top-level files: {', '.join(top_files[:40])}"]
    for d, n in sorted(dirs.items()):
        if n >= 2:
            lines.append(f"{d}  ({n} files)")
        if len(lines) >= max_lines:
            lines.append("...")
            break
    return "\n".join(lines)


def conventions(root: Path, cfg: Config) -> str:
    budget = cfg.get("context.max_instruction_chars", 12000)
    chunks = []
    for name in cfg.get("context.instruction_files", []):
        p = root / name
        if p.is_file():
            text = p.read_text(errors="replace").strip()
            chunks.append(f"--- {name} ---\n{text[: budget // 2]}")
    joined = "\n\n".join(chunks)
    return joined[:budget] if joined else "(no instruction files found)"


def commands(cfg: Config) -> str:
    lines = []
    for p in cfg.packages:
        for kind, cmd in sorted(p.commands.items()):
            if kind != "setup":
                lines.append(f"- package {p.name} (root {p.root}, owns {', '.join(p.triggers)}): {kind} -> {cmd}")
    return "\n".join(lines) or "- none configured"
