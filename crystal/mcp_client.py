"""Thin MCP client pool: one session per registered server, results parsed from JSON-in-text.

The registry is the effective one for the current workspace (crystal/registry.py: built-ins < ~/.claude.json <
~/.mcp.json < <workspace>/.mcp.json). Transports, chosen per entry:
- stdio (default): spawns `command args...` as a subprocess with the entry's cwd and env, same as Claude Code does
  from .mcp.json. Always available; what real local MCP servers use (npx ..., uvx ..., python ...).
- http (`type: http`, streamable HTTP) and sse (`type: sse`): remote servers by URL, optional `headers` (sent on
  every request through a custom httpx client).
- in-process (CRYSTAL_INPROCESS=1, or `ServerPool(inprocess=True)`): for a stdio entry that is a python module
  (`module`: the built-ins, or `python -m pkg.mod`) or a python script file (loaded by path, its directory on
  sys.path as if run), imports it and connects to its MCPServer instance directly over
  `mcp.client._memory.InMemoryTransport` (no subprocess, no per-server mcp/pydantic import). A module that defines
  `configure(args)` gets the entry's args (the built-in code/git servers read `--root` from them). Used by the test
  suite (see tests/conftest.py) to avoid spawning 9 subprocesses per pool. Entries that are neither (npx/uvx
  servers, remote servers) use their real transport regardless of the flag.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client



def load_registry(workspace=None) -> dict[str, dict]:
    """The effective registry for the current workspace (or `workspace`)."""
    from crystal.registry import effective_registry
    return effective_registry(workspace)


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


def transport_for(spec: dict, inprocess: bool) -> str:
    """Which transport `ServerPool.session` will use for an entry: inprocess | stdio | http | sse."""
    from crystal.registry import inprocess_target, transport_of
    kind = spec.get("transport") or transport_of(spec)
    if kind == "stdio" and inprocess and inprocess_target(spec) is not None:
        return "inprocess"
    return kind


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

    def transport(self, server: str) -> str:
        return transport_for(self.registry[server], self.inprocess)

    async def _connect(self, server: str, spec: dict):
        kind = transport_for(spec, self.inprocess)
        if kind == "inprocess":
            from crystal.registry import import_target, inprocess_target
            module = import_target(inprocess_target(spec))
            if hasattr(module, "configure"):
                module.configure(list(spec.get("args") or []))
            mcp_server = getattr(module, spec.get("attr", "mcp"))
            return await self._stack.enter_async_context(InMemoryTransport(mcp_server))
        if kind == "stdio":
            params = StdioServerParameters(command=spec["command"], args=list(spec.get("args") or []),
                                           cwd=spec.get("cwd") or os.getcwd(),
                                           env={**os.environ, **spec.get("env", {})})
            return await self._stack.enter_async_context(stdio_client(params))
        if kind == "http":
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            client = await self._stack.enter_async_context(create_mcp_http_client(headers=spec.get("headers")))
            streams = await self._stack.enter_async_context(streamable_http_client(spec["url"], http_client=client))
            return streams[0], streams[1]
        if kind == "sse":
            from mcp.client.sse import sse_client
            streams = await self._stack.enter_async_context(sse_client(spec["url"], headers=spec.get("headers")))
            return streams[0], streams[1]
        raise ValueError(f"{server}: unknown transport {kind!r}")

    async def session(self, server: str) -> ClientSession:
        if server in self._sessions:
            return self._sessions[server]
        if server not in self.registry:
            raise KeyError(f"no MCP server named {server!r} (registered: {', '.join(sorted(self.registry)) or 'none'})")
        lock = self._locks.setdefault(server, asyncio.Lock())
        async with lock:
            if server in self._sessions:
                return self._sessions[server]
            read, write = await self._connect(server, self.registry[server])
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
