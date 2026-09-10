"""The Claude Code hooks that record sessions, and their installation into ~/.claude/settings.json.

Three hooks, all running `mcp-explorer hook` (crystal/trace/record.py: hook_main), which maps the hook's `cwd` to a
workspace and appends to that workspace's trace dir under $MCP_EXPLORER_HOME:
  PostToolUse       matcher mcp__.*|Read|Grep|Glob|Bash    every MCP call; codebase reads inside an investigation
  UserPromptSubmit                                          the prompt (the input a flow induced later will take)
  Stop                                                      the agent's final message

`install(settings_path)` merges them into the user's settings (idempotent: our entries are recognised by their
command and replaced, everything else in the file is kept); `uninstall` removes exactly ours. `mcp-explorer record`
writes the same hooks to a temporary settings file for a headless run.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

MATCHER = "mcp__.*|Read|Grep|Glob|Bash"
EVENTS = ("PostToolUse", "UserPromptSubmit", "Stop")
MARK = "mcp-explorer-hook"     # what identifies our entries in a settings file
SETTINGS_ENV = "CRYSTAL_CLAUDE_SETTINGS"   # overrides the settings file path (tests point it at nothing)


def default_settings_path() -> Path:
    override = os.environ.get(SETTINGS_ENV)
    return Path(override).expanduser() if override else Path.home() / ".claude" / "settings.json"


def hook_command(python: str | None = None) -> str:
    """`<this python> -m crystal.cli hook`: works wherever the package is installed (uv run, uvx, pip)."""
    return f"{shlex.quote(python or sys.executable)} -m crystal.cli hook"


def hook_entries(command: str | None = None) -> dict[str, list[dict]]:
    cmd = command or hook_command()
    hook = {"type": "command", "command": cmd, "mcp-explorer": True}
    return {
        "PostToolUse": [{"matcher": MATCHER, "hooks": [dict(hook)]}],
        "UserPromptSubmit": [{"hooks": [dict(hook)]}],
        "Stop": [{"hooks": [dict(hook)]}],
    }


def is_ours(hook: dict) -> bool:
    cmd = str(hook.get("command", "")) if isinstance(hook, dict) else ""
    return bool(hook.get("mcp-explorer")) if isinstance(hook, dict) and "mcp-explorer" in hook else \
        ("crystal.cli hook" in cmd or "mcp-explorer hook" in cmd or MARK in cmd)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text().strip()
    if not text:
        return {}
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return doc


def _strip_ours(hooks: dict) -> dict:
    """The hooks section without our entries; groups left empty are dropped."""
    out = {}
    for event, groups in (hooks or {}).items():
        kept = []
        for g in groups if isinstance(groups, list) else []:
            inner = [h for h in (g.get("hooks") or []) if not is_ours(h)] if isinstance(g, dict) else []
            if inner:
                kept.append({**g, "hooks": inner})
        if kept:
            out[event] = kept
    return out


def merged(doc: dict, command: str | None = None) -> dict:
    hooks = _strip_ours(doc.get("hooks") or {})
    for event, groups in hook_entries(command).items():
        hooks.setdefault(event, []).extend(groups)
    return {**doc, "hooks": hooks}


def install(settings_path: Path | None = None, command: str | None = None) -> Path:
    path = settings_path or default_settings_path()
    doc = merged(_load(path), command)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def uninstall(settings_path: Path | None = None) -> Path:
    path = settings_path or default_settings_path()
    doc = _load(path)
    if "hooks" in doc:
        hooks = _strip_ours(doc["hooks"])
        if hooks:
            doc["hooks"] = hooks
        else:
            del doc["hooks"]
        path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def installed(settings_path: Path | None = None) -> dict[str, bool]:
    """Which of our hooks the settings file carries."""
    path = settings_path or default_settings_path()
    try:
        hooks = _load(path).get("hooks") or {}
    except (ValueError, json.JSONDecodeError):
        hooks = {}
    out = {}
    for event in EVENTS:
        out[event] = any(is_ours(h) for g in (hooks.get(event) or []) if isinstance(g, dict) for h in (g.get("hooks") or []))
    return out


def write_temp_settings(path: Path, command: str | None = None) -> Path:
    """A settings file holding only our hooks (for `claude --settings <file>` in a headless run)."""
    path.write_text(json.dumps({"hooks": hook_entries(command)}, indent=2) + "\n")
    return path


def main(args: list[str]) -> int:
    """install-hook [--uninstall] [--settings <path>] [--command <cmd>] [--status]"""
    settings = Path(args[args.index("--settings") + 1]).expanduser() if "--settings" in args else default_settings_path()
    command = args[args.index("--command") + 1] if "--command" in args else None
    if "--status" in args:
        st = installed(settings)
        for event, ok in st.items():
            print(f"{event:18} {'installed' if ok else 'not installed'}")
        print(f"settings: {settings}")
        return 0 if all(st.values()) else 1
    if "--uninstall" in args:
        uninstall(settings)
        print(f"removed the mcp-explorer hooks from {settings}")
        return 0
    install(settings, command)
    print(f"installed the mcp-explorer hooks ({', '.join(EVENTS)}) in {settings}\n"
          f"command: {command or hook_command()}\n"
          "Every Claude Code session now records its prompt, MCP calls and final message into "
          "$MCP_EXPLORER_HOME/workspaces/<workspace>/traces/. Run `mcp-explorer` in a repo to see them.")
    return 0
