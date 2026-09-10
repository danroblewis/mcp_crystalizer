"""Generic git server over a workspace (tool shapes after the reference git MCP, plus git_grep and git_blame).

  .venv/bin/python crystal/servers/git.py --root /path/to/workspace
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crystal.servers import RootFn, RootHolder, root_from, text  # noqa: E402


def make_server(root: RootFn | Path, name: str = "git") -> MCPServer:
    root_fn: RootFn = root if callable(root) else (lambda r=Path(root): r)
    mcp = MCPServer(name, instructions="Git over the workspace. git_log supports since/until/grep/path; git_show; git_grep; git_blame.")

    def git(*args) -> str:
        p = subprocess.run(["git", *args], cwd=root_fn(), capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else f"error: {p.stderr.strip()}"

    @mcp.tool(structured_output=False, name="git_log",
              description="Commit log. Optional since/until (ISO-8601), grep (message regex), path (limit to a path). max_count default 10.")
    def git_log(max_count: int = 10, since: str = "", until: str = "", grep: str = "", path: str = "") -> str:
        args = ["log", f"-n{int(max_count)}", "--date=iso-strict", "--format=%H%x1f%an%x1f%ad%x1f%s"]
        if since:
            args.append(f"--since={since}")
        if until:
            args.append(f"--until={until}")
        if grep:
            args += ["-i", f"--grep={grep}"]
        if path:
            args += ["--", path]
        out = git(*args)
        if out.startswith("error:"):
            return text({"commits": [], "error": out})
        commits = [dict(zip(["sha", "author", "date", "subject"], line.split("\x1f"))) for line in out.splitlines() if "\x1f" in line]
        return text({"commits": commits})

    @mcp.tool(structured_output=False, name="git_show", description="Show a commit (metadata + diff).")
    def git_show(sha: str) -> str:
        return git("show", "--stat", "-p", "--date=iso-strict", sha)

    @mcp.tool(structured_output=False, name="git_grep", description="Search tracked files for a regex. Returns file, line number and line.")
    def git_grep(pattern: str, path: str = "") -> str:
        args = ["grep", "-n", "-I", "-E", "-e", pattern]
        if path:
            args += ["--", path]
        out = git(*args)
        matches = []
        for line in out.splitlines():
            f, _, rest = line.partition(":")
            ln, _, content = rest.partition(":")
            if ln.isdigit():
                matches.append({"file": f, "line": int(ln), "text": content})
        return text({"matches": matches})

    @mcp.tool(structured_output=False, name="git_blame", description="Blame a file; optional line range 'start,end'.")
    def git_blame(path: str, lines: str = "") -> str:
        args = ["blame", "--date=short"]
        if lines:
            args += ["-L", lines]
        return git(*args, "--", path)

    return mcp


ROOT = RootHolder()
mcp = make_server(ROOT)


def configure(args: list[str]) -> None:
    """In-process transport: take `--root` from the registry entry's args."""
    ROOT.root = root_from(args)


if __name__ == "__main__":
    ROOT.root = root_from()
    mcp.run()
