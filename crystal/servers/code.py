"""Generic codebase server: grep / glob / read_file / codeowners over a workspace (what an agent's Read/Grep/Glob do).

  .venv/bin/python crystal/servers/code.py --root /path/to/workspace
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crystal.servers import RootFn, RootHolder, root_from, text  # noqa: E402

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", "dist", "build", ".next", ".cache"}
DEFAULT_GLOB = "**/*"
MAX_FILE_BYTES = 2_000_000


def make_server(root: RootFn | Path, name: str = "code", default_glob: str = DEFAULT_GLOB) -> MCPServer:
    """`default_glob` is what grep searches when no glob is given (the sim keeps its historical **/*.py)."""
    root_fn: RootFn = root if callable(root) else (lambda r=Path(root): r)
    mcp = MCPServer(name, instructions="Codebase search over the workspace: grep (regex), glob, read_file, codeowners.")

    def files(pattern: str = DEFAULT_GLOB):
        base = root_fn()
        for p in sorted(base.glob(pattern)):
            if p.is_file() and not (set(p.relative_to(base).parts[:-1]) & SKIP_DIRS):
                yield base, p

    def read(p: Path) -> list[str] | None:
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                return None
            return p.read_text(errors="replace").splitlines()
        except OSError:
            return None

    @mcp.tool(structured_output=False, name="grep",
              description=f"Regex search over files. `glob` limits files (default {default_glob}, e.g. **/*.py or src/**/*.ts). "
                          "Returns file, line, text and up to `context` lines around.")
    def grep(pattern: str, glob: str = default_glob, context: int = 0, max_results: int = 100) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return text({"error": f"bad regex: {e}"})
        out = []
        for base, p in files(glob or default_glob):
            lines = read(p)
            if lines is None:
                continue
            for i, line in enumerate(lines):
                if rx.search(line):
                    item = {"file": str(p.relative_to(base)), "line": i + 1, "text": line}
                    if context:
                        item["context"] = lines[max(0, i - context): i + context + 1]
                    out.append(item)
                    if len(out) >= max_results:
                        return text({"matches": out, "truncated": True})
        return text({"matches": out, "truncated": False})

    @mcp.tool(structured_output=False, name="glob", description="List files matching a glob, e.g. src/**/*.ts.")
    def glob_files(pattern: str) -> str:
        return text({"files": [str(p.relative_to(base)) for base, p in files(pattern)]})

    @mcp.tool(structured_output=False, name="read_file", description="Read a file (optionally lines start..end, 1-based).")
    def read_file(path: str, start: int = 1, end: int = 0) -> str:
        base = root_fn()
        p = (base / path).resolve()
        if base not in p.parents or not p.is_file():
            return text({"error": "not found"})
        lines = read(p)
        if lines is None:
            return text({"error": "unreadable"})
        end = end or len(lines)
        return text({"path": path, "start": start, "end": end, "content": "\n".join(lines[start - 1:end])})

    @mcp.tool(structured_output=False, name="codeowners", description="Owning team(s) for a path, from CODEOWNERS.")
    def codeowners(path: str) -> str:
        base = root_fn()
        rules_file = next((base / c for c in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS") if (base / c).is_file()), None)
        if rules_file is None:
            return text({"path": path, "owners": [], "error": "no CODEOWNERS file"})
        rules = [l.split() for l in rules_file.read_text().splitlines() if l.strip() and not l.startswith("#")]
        owner = None
        for pat, *owners in rules:
            if pat == "*" or path.startswith(pat.lstrip("/").rstrip("/")):
                owner = owners
        return text({"path": path, "owners": owner or []})

    return mcp


ROOT = RootHolder()
mcp = make_server(ROOT)


def configure(args: list[str]) -> None:
    """In-process transport: take `--root` from the registry entry's args."""
    ROOT.root = root_from(args)


if __name__ == "__main__":
    ROOT.root = root_from()
    mcp.run()
