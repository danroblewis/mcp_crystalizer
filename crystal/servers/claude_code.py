"""Our own small versions of Claude Code's own tools, so a flow induced from a recorded session can run.

A transcript is full of `Read`, `Grep`, `Glob` and `Bash` calls. Those are not MCP tools -- they live inside Claude
Code -- so a flow that replays them has nowhere to send them. This server answers them under the name and argument
shapes the transcripts use (`claude-code.Read{file_path, offset, limit}`, `claude-code.Grep{pattern, glob, type,
output_mode, head_limit}`, `claude-code.Glob{pattern, path}`, `claude-code.Bash{command}`). Claude Code's dashed
grep flags (-i, -A, -B, -C) are not valid parameter names, so `builtin_map.normalise_args` renames them to
`ignore_case` and `context` when a session is imported.

  python -m crystal.servers.claude_code --root /path/to/workspace

**Bash is read-only.** A recorded command is replayed only when it is recognisably a query: its program is on the
allowlist below, it has no redirection, and no segment of a pipeline writes. Anything else is refused with an
explanation rather than run, because a flow runs unattended and a session's `rm -rf build` must never be replayed.
Set `CRYSTAL_ALLOW_SHELL=1` to lift the check (never the default, and the refusal names the variable).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from crystal.servers import RootFn, RootHolder, root_from, text
from crystal.servers.code import SKIP_DIRS

# Programs that only read. `git` is here for its read-only subcommands, which are checked separately.
READ_ONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "find", "file", "stat", "du", "df",
    "echo", "pwd", "date", "which", "type", "basename", "dirname", "realpath", "readlink", "sort", "uniq",
    "cut", "tr", "awk", "sed", "jq", "yq", "column", "diff", "cmp", "md5", "shasum", "tree", "env", "printenv",
    "git", "python3", "python", "node", "uv", "npm", "pip", "cargo", "go",
}
# Subcommands that make those language/package tools safe to replay; anything else is refused.
SAFE_SUB = {
    "git": {"log", "show", "grep", "blame", "diff", "status", "branch", "tag", "describe", "rev-parse",
            "ls-files", "ls-tree", "shortlog", "config", "remote", "cat-file", "count-objects"},
    "npm": {"ls", "list", "view", "outdated"}, "pip": {"list", "show", "freeze"},
    "uv": {"tree", "pip"}, "cargo": {"tree", "metadata"}, "go": {"list", "version", "env"},
}
INTERPRETERS = {"python", "python3", "node"}          # only with -c/-V/--version: never a script path
WRITE_TOKENS = re.compile(r"(^|\s)(>|>>|\||&&|;|\$\(|`)")    # redirection or chaining: not one read
MAX_OUTPUT = 200_000
DEFAULT_TIMEOUT = 60


class ShellRefused(Exception):
    pass


def check_command(cmd: str) -> list[str]:
    """The argv of a command safe to replay, or raise ShellRefused explaining why not."""
    cmd = (cmd or "").strip()
    if not cmd:
        raise ShellRefused("empty command")
    if os.environ.get("CRYSTAL_ALLOW_SHELL", "").strip() not in ("", "0", "false"):
        return shlex.split(cmd)
    if WRITE_TOKENS.search(cmd):
        raise ShellRefused("refused: the command redirects or chains (`>`, `|`, `&&`, `;`), so it is not a single read. "
                           "Set CRYSTAL_ALLOW_SHELL=1 to run recorded commands unchecked.")
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise ShellRefused(f"refused: cannot parse the command ({e})") from e
    prog = Path(argv[0]).name
    if prog not in READ_ONLY:
        raise ShellRefused(f"refused: `{prog}` is not a read-only program this server replays. "
                           f"Set CRYSTAL_ALLOW_SHELL=1 to run recorded commands unchecked.")
    sub = next((a for a in argv[1:] if not a.startswith("-")), None)
    if prog in SAFE_SUB:
        if sub not in SAFE_SUB[prog]:
            raise ShellRefused(f"refused: `{prog} {sub or ''}`.strip() is not one of the read-only subcommands "
                               f"({', '.join(sorted(SAFE_SUB[prog]))}).")
    elif prog in INTERPRETERS:
        flags = {a for a in argv[1:] if a.startswith("-")}
        if not (flags & {"-c", "-V", "--version"}):
            raise ShellRefused(f"refused: `{prog}` would run a script, which is the session's work rather than a query.")
    return argv


def make_server(root: RootFn | Path, name: str = "claude-code") -> MCPServer:
    root_fn: RootFn = root if callable(root) else (lambda r=Path(root): r)
    mcp = MCPServer(name, instructions="Claude Code's own tools (Read, Grep, Glob, Bash) over the workspace, so flows "
                                       "induced from recorded sessions can replay them. Bash is read-only.")

    def resolve(p: str) -> Path:
        base = root_fn()
        q = Path(p).expanduser()
        q = q if q.is_absolute() else base / q
        return q.resolve()

    def inside(p: Path) -> bool:
        base = root_fn().resolve()
        return p == base or base in p.parents

    def walk(pattern: str, path: str = ""):
        base = resolve(path) if path else root_fn()
        for p in sorted(base.glob(pattern or "**/*")):
            if p.is_file() and not (set(p.parts) & SKIP_DIRS):
                yield p

    @mcp.tool(structured_output=False, name="Read",
              description="Read a file. `file_path` absolute or relative to the workspace; `offset` first line "
                          "(1-based), `limit` how many lines. Returns numbered lines, as Claude Code does.")
    def read_tool(file_path: str, offset: int = 1, limit: int = 2000) -> str:
        p = resolve(file_path)
        if not inside(p):
            return text({"error": f"{file_path} is outside the workspace"})
        if not p.is_file():
            return text({"error": f"{file_path} not found"})
        try:
            lines = p.read_text(errors="replace").splitlines()
        except OSError as e:
            return text({"error": str(e)})
        start = max(1, int(offset or 1))
        chunk = lines[start - 1: start - 1 + max(1, int(limit or 2000))]
        body = "\n".join(f"{start + i:6}\t{ln}" for i, ln in enumerate(chunk))
        return text({"file_path": str(p), "lines": len(lines), "offset": start, "content": body[:MAX_OUTPUT]})

    @mcp.tool(structured_output=False, name="Grep",
              description="Regex search over the workspace. `glob` or `type` (py, ts, ...) limits files; "
                          "`output_mode` files_with_matches | content | count; `ignore_case`; "
                          "`context` lines around each hit; `head_limit` caps results. Claude Code's dashed flags "
                          "(-i, -A, -B, -C) are normalised to these when a session is imported.")
    def grep_tool(pattern: str, path: str = "", glob: str = "", type: str = "", output_mode: str = "files_with_matches",
                  head_limit: int = 100, ignore_case: bool = False, context: int = 0) -> str:
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as e:
            return text({"error": f"bad pattern: {e}"})
        pat = glob or (f"**/*.{type}" if type else "**/*")
        ctx = int(context or 0)
        limit = max(1, int(head_limit or 100))
        files, matches, count = [], [], 0
        for p in walk(pat, path):
            try:
                lines = p.read_text(errors="replace").splitlines()
            except OSError:
                continue
            hits = [(i, ln) for i, ln in enumerate(lines) if rx.search(ln)]
            if not hits:
                continue
            count += len(hits)
            files.append(str(p))
            if output_mode == "content":
                for i, ln in hits:
                    item = {"file": str(p), "line": i + 1, "text": ln}
                    if ctx:
                        item["context"] = lines[max(0, i - ctx): i + ctx + 1]
                    matches.append(item)
                    if len(matches) >= limit:
                        break
            if len(files) >= limit and output_mode != "content":
                break
        if output_mode == "count":
            return text({"count": count, "files": len(files)})
        if output_mode == "content":
            return text({"matches": matches[:limit]})
        return text({"files": files[:limit]})

    @mcp.tool(structured_output=False, name="Glob",
              description="List files matching a glob, e.g. `src/**/*.ts`. `path` limits the search to a subdirectory.")
    def glob_tool(pattern: str, path: str = "") -> str:
        return text({"files": [str(p) for p in walk(pattern, path)][:1000]})

    @mcp.tool(structured_output=False, name="Bash",
              description="Run a recorded shell command in the workspace. READ-ONLY: a command that redirects, "
                          "chains, or runs anything but an allowlisted read-only program is refused rather than run.")
    def bash_tool(command: str, timeout: int = 0, description: str = "") -> str:
        try:
            argv = check_command(command)
        except ShellRefused as e:
            return text({"error": str(e), "command": command})
        try:
            proc = subprocess.run(argv, cwd=root_fn(), capture_output=True, text=True,
                                  timeout=min(int(timeout or 0) / 1000 or DEFAULT_TIMEOUT, 600))
        except subprocess.TimeoutExpired:
            return text({"error": "timed out", "command": command})
        except (OSError, ValueError) as e:
            return text({"error": str(e), "command": command})
        out = (proc.stdout or "")[:MAX_OUTPUT]
        return text({"command": command, "exit_code": proc.returncode, "stdout": out,
                     "stderr": (proc.stderr or "")[:4000]})

    return mcp


ROOT = RootHolder()
mcp = make_server(ROOT)


def configure(args: list[str]) -> None:
    """In-process transport: take `--root` from the registry entry's args."""
    ROOT.root = root_from(args)


if __name__ == "__main__":
    ROOT.root = root_from()
    mcp.run()
