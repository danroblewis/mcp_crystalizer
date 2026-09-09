"""Trace recorder.

Two entry points write the same JSONL record shape:
  * `python -m crystal.trace.record` as a Claude Code PostToolUse hook (reads the hook JSON on stdin)
  * `Recorder.record(...)` from the scripted agent / runner

Record: {ts, session_id, source, seq, server, tool, input, output, output_text, is_error, duration_ms}
Raw outputs double as cassettes for replay tests.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crystal import PROJECT_ROOT

TRACE_DIR = PROJECT_ROOT / "traces"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Recorder:
    def __init__(self, session_id: str, source: str, trace_dir: Path | None = None, meta: dict | None = None):
        self.session_id = session_id
        self.source = source
        self.dir = trace_dir or TRACE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{session_id}.jsonl"
        self.seq = 0
        if meta and not self.path.exists():
            self._write({"ts": _now(), "session_id": session_id, "source": source, "kind": "meta", **meta})

    def _write(self, rec: dict) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def record(self, server: str, tool: str, inp: dict, output: Any, output_text: str = "", is_error: bool = False,
               duration_ms: float | None = None, extra: dict | None = None) -> dict:
        self.seq += 1
        rec = {"ts": _now(), "session_id": self.session_id, "source": self.source, "kind": "call", "seq": self.seq,
               "server": server, "tool": tool, "input": inp, "output": output, "output_text": output_text,
               "is_error": is_error, "duration_ms": duration_ms, **(extra or {})}
        self._write(rec)
        return rec

    def note(self, text: str, **kw) -> None:
        self._write({"ts": _now(), "session_id": self.session_id, "source": self.source, "kind": "note", "text": text, **kw})


def split_tool_name(name: str) -> tuple[str, str]:
    """Claude Code names MCP tools mcp__<server>__<tool>."""
    if name.startswith("mcp__"):
        _, server, tool = name.split("__", 2)
        return server, tool
    return "claude-code", name


def hook_main() -> int:
    """PostToolUse hook: stdin carries {session_id, tool_name, tool_input, tool_response, transcript_path, cwd, ...}."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    name = payload.get("tool_name", "")
    server, tool = split_tool_name(name)
    if server == "claude-code" and tool not in ("Read", "Grep", "Glob", "Bash"):
        return 0  # only MCP calls and codebase reads are investigation steps
    resp = payload.get("tool_response")
    output_text = ""
    output: Any = resp
    if isinstance(resp, dict) and isinstance(resp.get("content"), list):
        output_text = "\n".join(c.get("text", "") for c in resp["content"] if isinstance(c, dict) and c.get("type") == "text")
    elif isinstance(resp, str):
        output_text = resp
    if output_text:
        try:
            output = json.loads(output_text)
        except json.JSONDecodeError:
            output = output_text
    rec = Recorder(payload.get("session_id", "unknown"), "claude-code",
                   meta={"transcript_path": payload.get("transcript_path"), "cwd": payload.get("cwd")})
    # seq continues across hook invocations: count existing call lines
    rec.seq = sum(1 for line in rec.path.open() if '"kind": "call"' in line) if rec.path.exists() else 0
    rec.record(server, tool, payload.get("tool_input") or {}, output, output_text,
               is_error=bool(isinstance(resp, dict) and resp.get("isError")),
               extra={"tool_use_id": payload.get("tool_use_id")})
    return 0


if __name__ == "__main__":
    sys.exit(hook_main())
