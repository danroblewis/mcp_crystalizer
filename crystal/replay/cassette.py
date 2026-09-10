"""Cassette record/replay for MCP calls, so flow regression tests run with no network and no servers.

A cassette maps (server, tool, canonical args) -> {output, output_text, is_error}. It can be built from a
live recording pass or directly from a recorded agent trace (the raw responses the agent saw).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from crystal import state


def current_cassette_dir() -> Path:
    return state.cassette_dir()


def call_key(server: str, tool: str, args: dict) -> str:
    canon = json.dumps({"s": server, "t": tool, "a": args}, sort_keys=True, default=str)
    return hashlib.sha1(canon.encode()).hexdigest()[:16]


class Cassette:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path and path.exists():
            self.entries = json.loads(path.read_text())

    @classmethod
    def from_trace(cls, session, path: Path | None = None) -> "Cassette":
        c = cls(path)
        for call in session.calls:
            c.put(call["server"], call["tool"], call.get("input") or {}, call.get("output"), call.get("output_text", ""), bool(call.get("is_error")))
        return c

    def put(self, server, tool, args, output, output_text="", is_error=False):
        self.entries[call_key(server, tool, args)] = {"server": server, "tool": tool, "args": args, "output": output,
                                                      "output_text": output_text, "is_error": is_error}

    def get(self, server, tool, args) -> dict | None:
        return self.entries.get(call_key(server, tool, args))

    def save(self, path: Path | None = None):
        p = path or self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.entries, indent=1, default=str))
        self.path = p


class CassettePool:
    """Drop-in for ServerPool. mode='replay' answers only from the cassette; mode='record' calls through
    `live` and stores; mode='auto' replays when present, else records."""

    def __init__(self, cassette: Cassette, live=None, mode: str = "replay"):
        self.cassette = cassette
        self.live = live
        self.mode = mode
        self.misses: list[dict] = []
        self.registry = getattr(live, "registry", {})

    async def __aenter__(self):
        if self.live:
            await self.live.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self.live:
            await self.live.__aexit__(*exc)

    async def list_tools(self, server: str):
        if self.live:
            return await self.live.list_tools(server)
        return []

    async def call_raw(self, server: str, tool: str, args: dict | None = None):
        args = args or {}
        hit = self.cassette.get(server, tool, args) if self.mode in ("replay", "auto") else None
        if hit is not None:
            return hit["output"], hit["output_text"], hit["is_error"]
        if self.mode == "replay" or not self.live:
            self.misses.append({"server": server, "tool": tool, "args": args})
            raise RuntimeError(f"cassette miss: {server}.{tool} {json.dumps(args, sort_keys=True, default=str)[:200]}")
        out, raw, err = await self.live.call_raw(server, tool, args)
        self.cassette.put(server, tool, args, out, raw, err)
        return out, raw, err

    async def call(self, server: str, tool: str, args: dict | None = None):
        out, _, err = await self.call_raw(server, tool, args)
        if err:
            raise RuntimeError(f"{server}.{tool} error: {out}")
        return out
