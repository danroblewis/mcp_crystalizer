"""The workspace: the directory this tool is pointed at, exactly the way Claude Code is pointed at a directory.

A workspace is the current directory unless `--workspace <dir>` or $CRYSTAL_WORKSPACE says otherwise. It supplies
  * the root the built-in `code` and `git` servers search,
  * its `.mcp.json`, the top layer of the server registry (crystal/registry.py),
  * its state dir under $MCP_EXPLORER_HOME (crystal/state.py): flows, runs, traces, cassettes, catalog, lifecycle.

Nothing here is special about this package's own source checkout: run in any directory, that directory is the
workspace. `activate()` puts the absolute path into $CRYSTAL_WORKSPACE so every child process (stdio servers, Claude
Code, the recording hook, the `flows` server) resolves the same workspace lazily.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ENV = "CRYSTAL_WORKSPACE"


@dataclass(frozen=True)
class Workspace:
    root: Path
    slug: str

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def state(self):
        from crystal.state import state_for
        return state_for(self.root)

    def mcp_json(self) -> Path:
        return self.root / ".mcp.json"

    def __str__(self) -> str:
        return f"{self.name} ({self.root})"

    def meta(self) -> dict:
        """Facts about the workspace an agent gets for free (Claude Code shows it the git remote): name, root,
        remote_url, repo_owner, repo_name, branch, head. Flows reference them as {{ workspace.repo_owner }}."""
        import subprocess
        out = {"name": self.name, "slug": self.slug, "root": str(self.root)}

        def git(*args):
            try:
                r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, timeout=5)
                return r.stdout.strip() if r.returncode == 0 else ""
            except Exception:  # noqa: BLE001
                return ""
        url = git("config", "--get", "remote.origin.url")
        if url:
            out["remote_url"] = url
            m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
            if m:
                out["repo_owner"], out["repo_name"] = m.group(1), m.group(2)
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch:
            out["branch"] = branch
        head = git("rev-parse", "HEAD")
        if head:
            out["head"] = head
        return out


def resolve_root(path: str | os.PathLike | None = None) -> Path:
    """An explicit path > $CRYSTAL_WORKSPACE > the current directory. Relative paths are taken from the cwd."""
    raw = str(path) if path not in (None, "") else os.environ.get(ENV, "").strip()
    p = Path(raw).expanduser() if raw else Path.cwd()
    return p.resolve()


def slug_of(root: Path) -> str:
    from crystal.state import slug_of as _slug
    return _slug(root)


def workspace(path: str | os.PathLike | None = None) -> Workspace:
    root = resolve_root(path)
    return Workspace(root=root, slug=slug_of(root))


def current() -> Workspace:
    """The workspace of this process ($CRYSTAL_WORKSPACE, else the current directory)."""
    return workspace(None)


def activate(path: str | os.PathLike | None) -> Workspace:
    """Make `path` the workspace of this process and of every child (stdio servers, Claude Code, its hook)."""
    ws = workspace(path)
    if not ws.root.is_dir():
        raise FileNotFoundError(f"workspace {ws.root} is not a directory")
    os.environ[ENV] = str(ws.root)
    return ws


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
