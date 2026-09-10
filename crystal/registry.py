"""The effective MCP server registry for a workspace: servers.yaml plus the mcp.json files, merged.

Layers, later wins per server name:
  1. servers.yaml               the project's defaults (the sim servers, the generic code/git servers, `flows`)
  2. ~/.mcp.json                the user's servers ($CRYSTAL_USER_MCP overrides the path; tests point it at nothing)
  3. <workspace>/.mcp.json      the workspace's servers; when the workspace is the project this is the project's
                                own .mcp.json, which pins `code` and `git` to the simulated repo

mcp.json is the common `mcpServers` format Claude Code, Claude Desktop and Cursor read:
  {"mcpServers": {"name": {"command": ..., "args": [...], "env": {...}}
                | {"type": "http" | "sse", "url": ..., "headers": {...}}}}
Values may use `${VAR}` and `${VAR:-default}` (expanded from the environment; an env entry that resolves to nothing
is dropped rather than exported empty) and the placeholders `{{workspace}}` / `{{project}}` (the workspace root and
the project root), so one file can be copied between workspaces. An entry of `null` or `{"disabled": true}` removes
a server an earlier layer defined (a real codebase's .mcp.json drops the simulated jira/slack/... this way).

Every entry is materialized on load: placeholders and variables expanded, relative commands and paths made absolute
against the file's base directory (the project for servers.yaml, the workspace for an mcp.json), `cwd` set to that
base, `transport` derived (stdio | http | sse) and, for a python script inside the project, `module` inferred so
the in-process transport can import it. `_source` names the file the entry came from. `to_mcp_json` turns the
registry back into a plain mcpServers document for Claude Code (private keys stripped).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from crystal import PROJECT_ROOT
from crystal.workspace import Workspace, current

SERVERS_YAML = PROJECT_ROOT / "servers.yaml"
USER_MCP_ENV = "CRYSTAL_USER_MCP"
PRIVATE_KEYS = ("module", "attr", "cwd", "transport", "_source")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_PYTHON = re.compile(r"^python[0-9.]*$")


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
    subs = {"workspace": str(ws.root), "project": str(PROJECT_ROOT), "workspace_slug": ws.slug}
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


def infer_module(command: str, args: list[str]) -> str | None:
    """`.venv/bin/python <project>/pkg/mod.py ...` -> `pkg.mod`, for the in-process transport."""
    if not args or not _PYTHON.match(Path(command).name):
        return None
    script = Path(args[0])
    if script.suffix != ".py" or not script.is_absolute():
        return None
    try:
        rel = script.resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return ".".join(rel.with_suffix("").parts)


def transport_of(spec: dict) -> str:
    t = str(spec.get("type") or spec.get("transport") or "").lower().replace("-", "_")
    if t in ("http", "streamable_http", "streamablehttp"):
        return "http"
    if t == "sse":
        return "sse"
    if t in ("", "stdio"):
        return "http" if spec.get("url") and not spec.get("command") else "stdio"
    raise ValueError(f"unknown MCP transport type {spec.get('type') or spec.get('transport')!r}")


def normalize(name: str, raw: dict, source: Path, base: Path, ws: Workspace, env: dict[str, str] | None = None) -> dict:
    """One materialized registry entry (see the module docstring)."""
    if not isinstance(raw, dict):
        raise ValueError(f"server {name!r} in {source}: expected an object, got {type(raw).__name__}")
    spec = expand_env(substitute(dict(raw), ws), env)
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


def read_mcp_json(path: Path) -> dict[str, dict]:
    doc = json.loads(path.read_text())
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if servers is None and isinstance(doc, dict):
        servers = doc.get("servers")   # a few tools use this key
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: expected {{\"mcpServers\": {{...}}}}")
    return servers


def read_servers_yaml(path: Path | None = None) -> dict[str, dict]:
    path = path or SERVERS_YAML
    return yaml.safe_load(path.read_text())["servers"]


def layers(ws: Workspace | None = None, servers_yaml: Path | None = None) -> list[tuple[Path, Path, dict[str, dict]]]:
    """(source file, base dir, raw entries) in load order. Missing files are skipped."""
    ws = ws or current()
    out = []
    y = servers_yaml or SERVERS_YAML
    if y.exists():
        out.append((y, y.parent, read_servers_yaml(y)))
    home = user_mcp_json()
    files = [home, ws.mcp_json()]
    seen = set()
    for f in files:
        try:
            key = f.resolve()
        except OSError:
            key = f
        if key in seen or not f.is_file():
            continue
        seen.add(key)
        out.append((f, ws.root, read_mcp_json(f)))
    return out


def effective_registry(ws: Workspace | None = None, servers_yaml: Path | None = None,
                       env: dict[str, str] | None = None) -> dict[str, dict]:
    """The merged, materialized registry for a workspace: later layers win per server name; an override with the
    same command and args as the entry it replaces keeps that entry's `module` (an mcp.json written by
    `crystal mcp-config` never carries one)."""
    ws = ws or current()
    reg: dict[str, dict] = {}
    for source, base, raw in layers(ws, servers_yaml):
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


def describe(name: str, spec: dict) -> str:
    """One line for `crystal servers`."""
    src = spec.get("_source", "?")
    try:
        src = str(Path(src).relative_to(PROJECT_ROOT))
    except ValueError:
        src = src.replace(str(Path.home()), "~")
    if spec.get("transport", "stdio") == "stdio":
        what = " ".join([spec["command"], *spec.get("args", [])]).replace(str(PROJECT_ROOT) + "/", "")
        if spec.get("module"):
            what += f"  [in-process: {spec['module']}]"
    else:
        what = spec["url"] + (f"  headers={sorted(spec['headers'])}" if spec.get("headers") else "")
    return f"{name:14} {spec.get('transport', 'stdio'):6} {src:36} {what}"
