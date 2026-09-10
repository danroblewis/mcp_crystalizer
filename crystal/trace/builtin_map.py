"""Map Claude Code's own tools onto the built-in `code` and `git` MCP servers.

Most recorded sessions never touch an MCP server: they read files, grep, glob and shell out to git. Those calls
describe real investigations and the built-in servers can replay them, so a trace is worth far more when they are
recorded as `code.read_file` / `git.git_log` than as opaque `claude-code.Read` / `claude-code.Bash`. Anything that
does not map (an arbitrary shell command, a file edit) stays under `claude-code` -- recorded for context, but never
offered as a flow step, because nothing could reproduce it safely.

Each mapping returns (server, tool, args) in the built-in server's own vocabulary, or None.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Callable

# `git log -n 20 --since=... -- path`, `git show <sha>`, `git grep -n <pat>`, `git blame <file>`
_GIT_SUB = {"log", "show", "grep", "blame"}


def _int(v: Any, default: int | None = None) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _read(inp: dict) -> tuple[str, str, dict] | None:
    path = inp.get("file_path") or inp.get("path")
    if not path:
        return None
    args: dict[str, Any] = {"path": str(path)}
    start = _int(inp.get("offset"))
    limit = _int(inp.get("limit"))
    if start:
        args["start"] = start
    if limit:
        args["end"] = (start or 1) + limit - 1
    return "code", "read_file", args


def _grep(inp: dict) -> tuple[str, str, dict] | None:
    pattern = inp.get("pattern")
    if not pattern:
        return None
    args: dict[str, Any] = {"pattern": str(pattern)}
    glob = inp.get("glob") or (f"**/*.{inp['type']}" if inp.get("type") else None)
    if glob:
        args["glob"] = str(glob)
    ctx = _int(inp.get("-C")) or _int(inp.get("-A")) or _int(inp.get("-B"))
    if ctx:
        args["context"] = ctx
    return "code", "grep", args


def _glob(inp: dict) -> tuple[str, str, dict] | None:
    pattern = inp.get("pattern")
    return ("code", "glob", {"pattern": str(pattern)}) if pattern else None


def _bash(inp: dict) -> tuple[str, str, dict] | None:
    """Only read-only git subcommands map; every other shell command is left alone on purpose."""
    cmd = str(inp.get("command") or "").strip()
    if not cmd or any(sep in cmd for sep in ("|", "&&", ";", ">", "$(", "`")):
        return None                                   # a pipeline is not one tool call
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    if len(parts) < 2 or parts[0] != "git" or parts[1] not in _GIT_SUB:
        return None
    sub, rest = parts[1], parts[2:]
    if sub == "log":
        args: dict[str, Any] = {}
        path = None
        skip = False
        for i, tok in enumerate(rest):
            if skip:                                   # this token was the value of the previous flag
                skip = False
                continue
            if tok == "--":
                path = rest[i + 1] if i + 1 < len(rest) else None
                break
            if tok == "-n" and i + 1 < len(rest):      # `-n 20` arrives as two tokens
                args["max_count"] = _int(rest[i + 1])
                skip = True
                continue
            m = re.fullmatch(r"-n(\d+)|--max-count=(\d+)", tok)
            if m:
                args["max_count"] = _int(m.group(1) or m.group(2))
            elif tok.startswith("--since="):
                args["since"] = tok.split("=", 1)[1]
            elif tok.startswith("--until="):
                args["until"] = tok.split("=", 1)[1]
            elif tok.startswith("--grep="):
                args["grep"] = tok.split("=", 1)[1]
            elif not tok.startswith("-") and path is None and "/" in tok:
                path = tok
        if path:
            args["path"] = path
        return "git", "git_log", args
    if sub == "show":
        sha = next((t for t in rest if not t.startswith("-")), None)
        return ("git", "git_show", {"sha": sha}) if sha else None
    if sub == "grep":
        pat = next((t for t in rest if not t.startswith("-")), None)
        return ("git", "git_grep", {"pattern": pat}) if pat else None
    if sub == "blame":
        path = next((t for t in rest if not t.startswith("-")), None)
        return ("git", "git_blame", {"path": path}) if path else None
    return None


MAPPERS: dict[str, Callable[[dict], tuple[str, str, dict] | None]] = {
    "Read": _read, "Grep": _grep, "Glob": _glob, "Bash": _bash,
}


def map_call(name: str, inp: dict | None) -> tuple[str, str, dict] | None:
    """(server, tool, args) on a built-in server for a Claude Code tool call, or None when nothing can replay it."""
    fn = MAPPERS.get(name)
    if not fn:
        return None
    try:
        return fn(inp or {})
    except Exception:  # noqa: BLE001 - a call we cannot read is simply not mapped
        return None


# Claude Code's own argument names for tools we answer ourselves (crystal/servers/claude_code.py): its dashed
# flags are not valid parameter names, so a recorded call is normalised to the ones our server declares.
FLAG_RENAMES = {"-i": "ignore_case", "-n": None, "-C": "context", "-A": "context", "-B": "context",
                "multiline": None, "output_mode": "output_mode", "head_limit": "head_limit"}


def normalise_args(tool: str, inp: dict | None) -> dict:
    """A recorded `claude-code.<tool>` call in the vocabulary our own server declares."""
    out: dict[str, Any] = {}
    for k, v in (inp or {}).items():
        if k in FLAG_RENAMES:
            key = FLAG_RENAMES[k]
            if key is None:
                continue                       # a display flag (-n, multiline): nothing to replay
            out.setdefault(key, v if not isinstance(v, bool) or key == "ignore_case" else v)
        else:
            out[k] = v
    if tool == "Grep" and isinstance(out.get("context"), bool):
        out.pop("context")
    return out


def is_replayable(server: str) -> bool:
    """A step a flow can actually execute: anything but the tools that only existed inside the agent's session."""
    return server != "claude-code"
