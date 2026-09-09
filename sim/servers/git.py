"""Simulated git MCP server over the real sim repo (tool shapes after the reference git MCP, plus git_grep)."""
import subprocess
from pathlib import Path
from mcp.server.mcpserver import MCPServer
from common import text

REPO = Path(__file__).resolve().parent.parent / "repo"
mcp = MCPServer("sim-git", instructions="Git over the sim monorepo. git_log supports since/until/grep/path; git_show; git_grep.")


def _git(*args) -> str:
    p = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else f"error: {p.stderr.strip()}"


@mcp.tool(structured_output=False, name="git_log", description="Commit log. Optional since/until (ISO-8601), grep (message regex), path (limit to a path). max_count default 10.")
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
    out = _git(*args)
    commits = [dict(zip(["sha", "author", "date", "subject"], line.split("\x1f"))) for line in out.splitlines() if "\x1f" in line]
    return text({"commits": commits})


@mcp.tool(structured_output=False, name="git_show", description="Show a commit (metadata + diff).")
def git_show(sha: str) -> str:
    return _git("show", "--stat", "-p", "--date=iso-strict", sha)


@mcp.tool(structured_output=False, name="git_grep", description="Search tracked files for a regex. Returns file, line number and line.")
def git_grep(pattern: str, path: str = "") -> str:
    args = ["grep", "-n", "-I", "-E", "-e", pattern]
    if path:
        args += ["--", path]
    out = _git(*args)
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
    return _git(*args, "--", path)


if __name__ == "__main__":
    mcp.run()
