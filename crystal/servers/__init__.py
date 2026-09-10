"""Generic MCP servers over any workspace directory: `code` (grep / glob / read_file / codeowners) and `git`
(git_log / git_show / git_grep / git_blame). Each takes `--root <dir>` (or $CRYSTAL_WORKSPACE, else the current
directory) and keeps the tool names the sim servers introduced, so flows written against the sim keep running.
Both expose `make_server(root)` so the sim wrappers (sim/servers/code.py, git.py) and tests can build an instance
over a fixed directory without touching the module-level one."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable


def text(obj) -> str:
    """Real vendor servers return JSON inside a text block; so do these."""
    return json.dumps(obj, indent=2, default=str)


def root_from(args: list[str] | None = None) -> Path:
    """`--root <dir>` / `--root=<dir>` from the args, else $CRYSTAL_WORKSPACE, else the current directory."""
    args = sys.argv[1:] if args is None else args
    for i, a in enumerate(args):
        if a == "--root" and i + 1 < len(args):
            return Path(args[i + 1]).expanduser().resolve()
        if a.startswith("--root="):
            return Path(a.split("=", 1)[1]).expanduser().resolve()
    ws = os.environ.get("CRYSTAL_WORKSPACE", "").strip()
    return Path(ws).expanduser().resolve() if ws else Path.cwd().resolve()


class RootHolder:
    """Mutable root for the module-level server: set by `configure(args)` (in-process transport) or at startup."""

    def __init__(self, root: Path | None = None):
        self.root = root

    def __call__(self) -> Path:
        if self.root is None:
            self.root = root_from()
        return self.root


RootFn = Callable[[], Path]
