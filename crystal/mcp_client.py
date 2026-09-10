"""Thin MCP client pool: one session per registered server, results parsed from JSON-in-text.

Two transports:
- stdio (default): spawns `.venv/bin/python sim/servers/<x>.py` as a subprocess, same as Claude Code (.mcp.json)
  and the UI use. Always available; the only transport for real (non-sim) MCP servers.
- in-process (CRYSTAL_INPROCESS=1, or `ServerPool(inprocess=True)`): imports the sim server's module and connects
  to its MCPServer instance directly over `mcp.client._memory.InMemoryTransport` (in-memory streams, no subprocess,
  no per-server mcp/pydantic import). Requires the registry entry to name a `module` (+ optional `attr`, default
  "mcp"). Used by the test suite (see tests/conftest.py) to avoid spawning 8 subprocesses per pool.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import yaml
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from crystal import PROJECT_ROOT


def load_registry(path: Path | None = None) -> dict[str, dict]:
    path = path or PROJECT_ROOT / "servers.yaml"
    return yaml.safe_load(path.read_text())["servers"]


def _inprocess_default() -> bool:
    return os.environ.get("CRYSTAL_INPROCESS", "").strip().lower() in ("1", "true", "yes")


def parse_result(result) -> Any:
    """CallToolResult -> python object. Prefers structuredContent, then JSON parsed from text blocks."""
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if sc and not (isinstance(sc, dict) and set(sc) == {"result"} and isinstance(sc["result"], str)):
        return sc
    texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text"]
    raw = "\n".join(texts)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


class ServerPool:
    """Async context manager holding live sessions. `await pool.call('jira', 'jira_get_issue', {...})`."""

    def __init__(self, registry: dict[str, dict] | None = None, only: set[str] | None = None,
                 inprocess: bool | None = None):
        self.registry = registry or load_registry()
        self.only = only
        self.inprocess = _inprocess_default() if inprocess is None else inprocess
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}   # one per server, so connecting to N servers concurrently
                                                     # (e.g. spawning N stdio subprocesses) doesn't serialize

    async def __aenter__(self):
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._stack.__aexit__(*exc)

    async def session(self, server: str) -> ClientSession:
        if server in self._sessions:
            return self._sessions[server]
        lock = self._locks.setdefault(server, asyncio.Lock())
        async with lock:
            if server in self._sessions:
                return self._sessions[server]
            spec = self.registry[server]
            if self.inprocess and spec.get("module"):
                module = importlib.import_module(spec["module"])
                mcp_server = getattr(module, spec.get("attr", "mcp"))
                read, write = await self._stack.enter_async_context(InMemoryTransport(mcp_server))
            else:
                params = StdioServerParameters(command=spec["command"], args=spec.get("args", []),
                                               cwd=spec.get("cwd", str(PROJECT_ROOT)),
                                               env={**os.environ, **spec.get("env", {})})
                read, write = await self._stack.enter_async_context(stdio_client(params))
            sess = await self._stack.enter_async_context(ClientSession(read, write))
            await sess.initialize()
            self._sessions[server] = sess
            return sess

    async def list_tools(self, server: str) -> list[dict]:
        sess = await self.session(server)
        res = await sess.list_tools()
        return [{"name": t.name, "description": t.description, "inputSchema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)} for t in res.tools]

    async def call(self, server: str, tool: str, args: dict | None = None) -> Any:
        sess = await self.session(server)
        res = await sess.call_tool(tool, args or {})
        if getattr(res, "is_error", False) or getattr(res, "isError", False):
            raise RuntimeError(f"{server}.{tool} error: {parse_result(res)}")
        return parse_result(res)

    async def call_raw(self, server: str, tool: str, args: dict | None = None):
        """Returns (parsed, raw_text, is_error) for recording."""
        sess = await self.session(server)
        res = await sess.call_tool(tool, args or {})
        texts = [c.text for c in getattr(res, "content", []) if getattr(c, "type", "") == "text"]
        return parse_result(res), "\n".join(texts), bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
