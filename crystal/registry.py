"""The effective MCP server registry for a workspace: built-in defaults plus the mcp.json files, merged.

Layers, later wins per server name:
  1. built-ins                  `code` and `git` (crystal/servers, over the workspace root) and `flows` (the
                                workspace's crystallized flows as tools), from code, never from a file
  2. ~/.claude.json             Claude Code's user config, its top-level `mcpServers` ($CRYSTAL_CLAUDE_CONFIG
                                overrides the path): servers a user already configured for Claude Code just work
  3. ~/.mcp.json                the user's servers ($CRYSTAL_USER_MCP overrides the path; tests point it at nothing)
  4. <workspace>/.mcp.json      the workspace's servers, the same file Claude Code reads in that directory

mcp.json is the common `mcpServers` format Claude Code, Claude Desktop and Cursor read:
  {"mcpServers": {"name": {"command": ..., "args": [...], "env": {...}}
                | {"type": "http" | "sse", "url": ..., "headers": {...}}}}
Values may use `${VAR}` and `${VAR:-default}` (expanded from the environment; an env entry that resolves to nothing
is dropped rather than exported empty) and the placeholder `{{workspace}}` (the workspace root; also
`{{workspace_slug}}`). `${MCP_EXPLORER_PYTHON}` is always defined: the interpreter this tool runs under, so a
workspace can declare a python server as `"command": "${MCP_EXPLORER_PYTHON:-python}"` and it runs with this tool's
dependencies under `uv run` and `uvx` alike (the sim example does). An entry of `null` or `{"disabled": true}`
removes a server an earlier layer defined.

Every entry is materialized on load: placeholders and variables expanded, relative commands and paths made absolute
against the file's base directory (the workspace for an mcp.json), `cwd` set to that base, `transport` derived
(stdio | http | sse) and, for a python server, `module` set (`python -m pkg.mod`, or a script inside this package)
so the in-process transport can import it; any other python script can still be loaded in-process by path
(`inprocess_target`). `_source` names where the entry came from. `to_mcp_json` turns the registry back into a plain
mcpServers document for Claude Code (private keys stripped).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from crystal import PACKAGE_DIR
from crystal.workspace import Workspace, current

USER_MCP_ENV = "CRYSTAL_USER_MCP"
CLAUDE_CONFIG_ENV = "CRYSTAL_CLAUDE_CONFIG"
PYTHON_ENV = "MCP_EXPLORER_PYTHON"
BUILTIN_SOURCE = "built-in"
PRIVATE_KEYS = ("module", "attr", "cwd", "transport", "_source")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_PYTHON = re.compile(r"^python[0-9.]*$")


# ---------------------------------------------------------------- built-ins
def builtin_servers(ws: Workspace) -> dict[str, dict]:
    """The servers every workspace has: generic code/git over the workspace root, and the flows server. Run as
    `<this python> -m crystal.servers.<name>` so they work wherever the package is installed (uv run, uvx)."""
    py = sys.executable
    return {
        "code": {"command": py, "args": ["-m", "crystal.servers.code", "--root", str(ws.root)], "module": "crystal.servers.code"},
        "git": {"command": py, "args": ["-m", "crystal.servers.git", "--root", str(ws.root)], "module": "crystal.servers.git"},
        "flows": {"command": py, "args": ["-m", "crystal.servers.flows"], "module": "crystal.servers.flows"},
    }


# ---------------------------------------------------------------- expansion
def expand_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """`${VAR}` -> its value ('' when unset), `${VAR:-default}` -> its value or the default. Recurses into
    lists and dicts; non-strings pass through."""
    env = os.environ if env is None else env
    if isinstance(value, str):
        return _VAR.sub(lambda m: env.get(m.group(1)) if env.get(m.group(1)) not in (None, "") else (m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    return value


def substitute(value: Any, ws: Workspace) -> Any:
    subs = {"workspace": str(ws.root), "workspace_slug": ws.slug}
    if isinstance(value, str):
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: subs.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [substitute(v, ws) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, ws) for k, v in value.items()}
    return value


def _absolutize(arg: str, base: Path) -> str:
    """A relative path that exists under `base` becomes absolute (so the entry works from any cwd); anything
    else (flags, package names, URLs, plain words) is left alone."""
    if not arg or arg.startswith("-") or os.path.isabs(arg) or "://" in arg:
        return arg
    if arg == "." or "/" in arg or arg.endswith(".py"):
        p = base / arg
        if p.exists():
            return os.path.normpath(str(p))   # no symlink resolution: .venv/bin/python must stay the venv's python
    return arg


def _relativize(arg: str, base: Path) -> str:
    """The inverse, for a file that lives in `base` and is read from there: a path under base becomes relative."""
    if os.path.isabs(arg):
        try:
            rel = Path(arg).relative_to(base)
            return str(rel) if str(rel) != "." else "."
        except ValueError:
            return arg
    return arg


def is_python(command: str) -> bool:
    return bool(_PYTHON.match(Path(command).name))


def infer_module(command: str, args: list[str]) -> str | None:
    """`python -m pkg.mod ...` -> `pkg.mod`; `python <this package>/servers/x.py` -> `crystal.servers.x`."""
    if not args or not is_python(command):
        return None
    if args[0] == "-m" and len(args) > 1:
        return args[1]
    script = Path(args[0])
    if script.suffix != ".py" or not script.is_absolute():
        return None
    try:
        rel = script.resolve().relative_to(PACKAGE_DIR)
    except ValueError:
        return None
    return "crystal." + ".".join(rel.with_suffix("").parts)


def inprocess_target(spec: dict) -> str | Path | None:
    """What the in-process transport would import for a stdio entry: a module name, the path of a python script,
    or None (npx/uvx servers, remote servers)."""
    if spec.get("transport", "stdio") != "stdio":
        return None
    if spec.get("module"):
        return spec["module"]
    args = list(spec.get("args") or [])
    if args and is_python(spec.get("command", "")) and args[0].endswith(".py") and Path(args[0]).is_file():
        return Path(args[0])
    return None


_loaded_scripts: dict[str, Any] = {}


def load_script(path: Path):
    """Import a python server script by path, the way running it would (its directory first on sys.path so bare
    sibling imports resolve), once per process."""
    key = str(Path(path).resolve())
    if key in _loaded_scripts:
        return _loaded_scripts[key]
    p = Path(key)
    if str(p.parent) not in sys.path:
        sys.path.insert(0, str(p.parent))
    name = "_mcp_explorer_script_" + re.sub(r"[^0-9A-Za-z_]", "_", key)
    spec = importlib.util.spec_from_file_location(name, p)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _loaded_scripts[key] = module
    return module


def import_target(target: str | Path):
    return load_script(target) if isinstance(target, Path) else importlib.import_module(target)


def transport_of(spec: dict) -> str:
    t = str(spec.get("type") or spec.get("transport") or "").lower().replace("-", "_")
    if t in ("http", "streamable_http", "streamablehttp"):
        return "http"
    if t == "sse":
        return "sse"
    if t in ("", "stdio"):
        return "http" if spec.get("url") and not spec.get("command") else "stdio"
    raise ValueError(f"unknown MCP transport type {spec.get('type') or spec.get('transport')!r}")


def _env_with_python(env: dict[str, str] | None) -> dict[str, str]:
    base = dict(os.environ if env is None else env)
    base.setdefault(PYTHON_ENV, sys.executable)
    return base


def normalize(name: str, raw: dict, source: Path | str, base: Path, ws: Workspace, env: dict[str, str] | None = None) -> dict:
    """One materialized registry entry (see the module docstring)."""
    if not isinstance(raw, dict):
        raise ValueError(f"server {name!r} in {source}: expected an object, got {type(raw).__name__}")
    spec = expand_env(substitute(dict(raw), ws), _env_with_python(env))
    transport = transport_of(spec)
    out: dict[str, Any] = {"transport": transport, "_source": str(source)}
    if transport == "stdio":
        if not spec.get("command"):
            raise ValueError(f"server {name!r} in {source}: stdio entry needs a `command`")
        cwd = Path(spec.get("cwd") or base)
        out["command"] = _absolutize(str(spec["command"]), cwd)
        out["args"] = [_absolutize(str(a), cwd) for a in (spec.get("args") or [])]
        raw_env = raw.get("env") or {}
        env_out = {k: str(v) for k, v in (spec.get("env") or {}).items()
                   if not (str(v) == "" and "${" in str(raw_env.get(k, "")))}   # unresolved ${VAR}: leave it unset
        if env_out:
            out["env"] = env_out
        out["cwd"] = str(cwd)
        module = spec.get("module") or infer_module(out["command"], out["args"])
        if module:
            out["module"] = module
            if spec.get("attr"):
                out["attr"] = spec["attr"]
    else:
        if not spec.get("url"):
            raise ValueError(f"server {name!r} in {source}: {transport} entry needs a `url`")
        out["type"] = transport
        out["url"] = str(spec["url"])
        if spec.get("headers"):
            out["headers"] = {k: str(v) for k, v in spec["headers"].items()}
    return out


# ---------------------------------------------------------------- sources
def user_mcp_json() -> Path:
    override = os.environ.get(USER_MCP_ENV)
    return Path(override).expanduser() if override else Path.home() / ".mcp.json"


def claude_user_config() -> Path:
    override = os.environ.get(CLAUDE_CONFIG_ENV)
    return Path(override).expanduser() if override else Path.home() / ".claude.json"


def read_mcp_json(path: Path) -> dict[str, dict]:
    doc = json.loads(path.read_text())
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if servers is None and isinstance(doc, dict):
        servers = doc.get("servers")   # a few tools use this key
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: expected {{\"mcpServers\": {{...}}}}")
    return servers


def read_claude_config(path: Path, workspace_root: Path | None = None) -> dict[str, dict]:
    """Claude Code's ~/.claude.json: its top-level `mcpServers` (user scope) plus, when `workspace_root` is given,
    the servers Claude Code stored for that project under `projects[<root>].mcpServers` (what `claude mcp add`
    writes for a project). Project entries win over user ones. Anything else in the file, and a file that is not
    what we expect, is ignored rather than an error: it is not ours."""
    try:
        doc = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(doc, dict):
        return {}
    servers = doc.get("mcpServers")
    out = dict(servers) if isinstance(servers, dict) else {}
    if workspace_root is not None:
        projects = doc.get("projects")
        if isinstance(projects, dict):
            wanted = {str(workspace_root), str(Path(workspace_root).resolve())}
            for proj_path, proj in projects.items():
                if str(proj_path) in wanted and isinstance(proj, dict) and isinstance(proj.get("mcpServers"), dict):
                    out.update(proj["mcpServers"])
    return out


def layers(ws: Workspace | None = None) -> list[tuple[Path | str, Path, dict[str, dict]]]:
    """(source, base dir, raw entries) in load order. Missing files are skipped."""
    ws = ws or current()
    out: list[tuple[Path | str, Path, dict[str, dict]]] = [(BUILTIN_SOURCE, ws.root, builtin_servers(ws))]
    claude = claude_user_config()
    if claude.is_file():
        servers = read_claude_config(claude, ws.root)
        if servers:
            out.append((claude, ws.root, servers))
    seen = set()
    for f in (user_mcp_json(), ws.mcp_json()):
        try:
            key = f.resolve()
        except OSError:
            key = f
        if key in seen or not f.is_file():
            continue
        seen.add(key)
        out.append((f, ws.root, read_mcp_json(f)))
    return out


def effective_registry(ws: Workspace | None = None, env: dict[str, str] | None = None) -> dict[str, dict]:
    """The merged, materialized registry for a workspace: later layers win per server name; an override with the
    same command and args as the entry it replaces keeps that entry's `module` (an mcp.json written by
    `mcp-explorer mcp-config` never carries one)."""
    ws = ws or current()
    reg: dict[str, dict] = {}
    for source, base, raw in layers(ws):
        for name, raw_spec in raw.items():
            if raw_spec is None or (isinstance(raw_spec, dict) and raw_spec.get("disabled")):
                reg.pop(name, None)   # `"jira": null` or `{"disabled": true}` in a later file drops a default
                continue
            spec = normalize(name, raw_spec, source, base, ws, env)
            old = reg.get(name)
            if old and "module" not in spec and old.get("module") and old.get("command") == spec.get("command") and old.get("args") == spec.get("args"):
                spec["module"] = old["module"]
                if old.get("attr"):
                    spec["attr"] = old["attr"]
            reg[name] = spec
    return reg


# ---------------------------------------------------------------- export
def to_mcp_json(registry: dict[str, dict], only: list[str] | None = None, relative_to: Path | None = None) -> dict:
    """A plain mcpServers document (what Claude Code reads): public keys only, absolute paths already in place.
    `relative_to` (the directory the file will be read from, i.e. the workspace) turns paths under it back into
    relative ones, so a stored .mcp.json is portable; the driver's temporary config keeps everything absolute."""
    servers = {}
    for name, spec in registry.items():
        if only is not None and name not in only:
            continue
        if spec.get("transport", "stdio") == "stdio":
            entry: dict[str, Any] = {"command": spec["command"], "args": list(spec.get("args") or [])}
            if relative_to is not None:
                entry["command"] = _relativize(entry["command"], relative_to)
                entry["args"] = [_relativize(a, relative_to) for a in entry["args"]]
            if spec.get("env"):
                entry["env"] = dict(spec["env"])
        else:
            entry = {"type": spec["transport"], "url": spec["url"]}
            if spec.get("headers"):
                entry["headers"] = dict(spec["headers"])
        servers[name] = entry
    return {"mcpServers": servers}


def describe(name: str, spec: dict, ws: Workspace | None = None) -> str:
    """One line for `mcp-explorer servers`."""
    ws = ws or current()
    src = str(spec.get("_source", "?"))
    if src != BUILTIN_SOURCE:
        try:
            src = str(Path(src).relative_to(ws.root))
        except ValueError:
            src = src.replace(str(Path.home()), "~")
    if spec.get("transport", "stdio") == "stdio":
        what = " ".join([spec["command"], *spec.get("args", [])]).replace(str(ws.root) + "/", "").replace(sys.executable, "python")
        if spec.get("module"):
            what += f"  [in-process: {spec['module']}]"
    else:
        what = spec["url"] + (f"  headers={sorted(spec['headers'])}" if spec.get("headers") else "")
    return f"{name:14} {spec.get('transport', 'stdio'):6} {src:36} {what}"
