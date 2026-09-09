"""Simulated codebase server: grep / glob / read over the sim repo (what an agent's Read/Grep/Glob do)."""
import re
from pathlib import Path
from mcp.server.mcpserver import MCPServer
from common import text

REPO = Path(__file__).resolve().parent.parent / "repo"
mcp = MCPServer("sim-code", instructions="Codebase search over the sim monorepo: grep (regex), glob, read_file.")


def _files(glob: str = "**/*"):
    for p in REPO.glob(glob):
        if p.is_file() and ".git" not in p.parts:
            yield p


@mcp.tool(structured_output=False, name="grep", description="Regex search over files. `glob` limits files (default **/*.py). Returns file, line, text and up to `context` lines around.")
def grep(pattern: str, glob: str = "**/*.py", context: int = 0, max_results: int = 100) -> str:
    rx = re.compile(pattern)
    out = []
    for p in _files(glob):
        lines = p.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            if rx.search(line):
                item = {"file": str(p.relative_to(REPO)), "line": i + 1, "text": line}
                if context:
                    item["context"] = lines[max(0, i - context): i + context + 1]
                out.append(item)
                if len(out) >= max_results:
                    return text({"matches": out, "truncated": True})
    return text({"matches": out, "truncated": False})


@mcp.tool(structured_output=False, name="glob", description="List files matching a glob, e.g. services/payments_api/**.")
def glob_files(pattern: str) -> str:
    return text({"files": sorted(str(p.relative_to(REPO)) for p in _files(pattern))})


@mcp.tool(structured_output=False, name="read_file", description="Read a file (optionally lines start..end, 1-based).")
def read_file(path: str, start: int = 1, end: int = 0) -> str:
    p = (REPO / path).resolve()
    if REPO not in p.parents or not p.is_file():
        return text({"error": "not found"})
    lines = p.read_text(errors="replace").splitlines()
    end = end or len(lines)
    return text({"path": path, "start": start, "end": end, "content": "\n".join(lines[start - 1:end])})


@mcp.tool(structured_output=False, name="codeowners", description="Owning team(s) for a path, from CODEOWNERS.")
def codeowners(path: str) -> str:
    rules = [l.split() for l in (REPO / "CODEOWNERS").read_text().splitlines() if l.strip()]
    owner = None
    for pat, *owners in rules:
        if path.startswith(pat.rstrip("/")):
            owner = owners
    return text({"path": path, "owners": owner or []})


if __name__ == "__main__":
    mcp.run()
