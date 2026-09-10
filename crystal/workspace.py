"""The workspace: the codebase this tool is pointed at, the way any AI agent is pointed at a directory.

A workspace is a directory. It supplies
  * the root the generic `code` and `git` servers search (`{{workspace}}` in servers.yaml args),
  * the `.mcp.json` that adds to / overrides the project's server registry (crystal/registry.py),
  * the namespace under which runs and traces are stored: runs/<slug>/, traces/<slug>/ (flows, cassettes,
    feedback and the lifecycle store stay in the project, keyed by flow name).

Resolution: an explicit path > $CRYSTAL_WORKSPACE > the project itself. A relative path is taken from the project
root, so `export CRYSTAL_WORKSPACE=workspaces/agentarena` means the same thing in every process (CLI, the flows MCP
server, the stdio servers the pool spawns, the hook Claude Code runs). The project as its own workspace is the
simulated world: slug `sim`, whose runs and traces are the top-level runs/ and traces/ directories, exactly where
they were before workspaces existed.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from crystal import PROJECT_ROOT

ENV = "CRYSTAL_WORKSPACE"
SIM_SLUG = "sim"


@dataclass(frozen=True)
class Workspace:
    root: Path
    slug: str

    @property
    def is_project(self) -> bool:
        return self.root == PROJECT_ROOT

    @property
    def name(self) -> str:
        return self.root.name

    def mcp_json(self) -> Path:
        return self.root / ".mcp.json"

    def namespaced(self, base: Path) -> Path:
        """runs/ and traces/ for this workspace: the base itself for the sim, base/<slug> for anything else."""
        return base if self.is_project else base / self.slug

    def __str__(self) -> str:
        return f"{self.slug} ({self.root})"


def resolve_root(path: str | os.PathLike | None = None) -> Path:
    raw = str(path) if path not in (None, "") else os.environ.get(ENV, "").strip()
    if not raw:
        return PROJECT_ROOT
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def slug_of(root: Path) -> str:
    if root == PROJECT_ROOT:
        return SIM_SLUG
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "workspace"
    if slug == SIM_SLUG:   # a real directory that happens to be called sim must not share the sim's namespace
        slug = f"{slug}-{hashlib.sha1(str(root).encode()).hexdigest()[:6]}"
    return slug


def workspace(path: str | os.PathLike | None = None) -> Workspace:
    root = resolve_root(path)
    return Workspace(root=root, slug=slug_of(root))


def current() -> Workspace:
    """The workspace of this process ($CRYSTAL_WORKSPACE or the project)."""
    return workspace(None)


def activate(path: str | os.PathLike | None) -> Workspace:
    """Make `path` the workspace of this process and of every child (stdio servers, Claude Code, its hook):
    the absolute path goes into $CRYSTAL_WORKSPACE, which everything else reads lazily."""
    ws = workspace(path)
    if not ws.root.is_dir():
        raise FileNotFoundError(f"workspace {ws.root} is not a directory")
    os.environ[ENV] = str(ws.root)
    return ws


def namespaced(base: Path, ws: Workspace | None = None) -> Path:
    return (ws or current()).namespaced(base)


def split_argv(argv: list[str]) -> tuple[str | None, list[str]]:
    """Pull `--workspace <dir>` / `--workspace=<dir>` out of a command line (anywhere: before or after the command).
    Returns (the value or None, the remaining args)."""
    out: list[str] = []
    ws = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--workspace" and i + 1 < len(argv):
            ws, i = argv[i + 1], i + 2
            continue
        if a.startswith("--workspace="):
            ws, i = a.split("=", 1)[1], i + 1
            continue
        out.append(a)
        i += 1
    return ws, out
