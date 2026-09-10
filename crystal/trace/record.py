"""Trace recorder.

Two entry points write the same JSONL record shape into the workspace's trace dir (crystal/state.py):
  * `mcp-explorer hook` as a Claude Code hook (reads the hook JSON on stdin; PostToolUse, UserPromptSubmit, Stop)
  * `Recorder.record(...)` from the scripted agent / runner / driver

Records, one per line, all with {ts, session_id, source, kind}:
  meta    {trigger, inputs, cwd, transcript_path, workspace, ...}   written once, first
  prompt  {text}                                                    what the user asked (UserPromptSubmit)
  call    {seq, server, tool, input, output, output_text, is_error, duration_ms}
  note    {text}
  result  {text}                                                    the agent's final message (Stop)
Raw outputs double as cassettes for replay tests.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crystal import state
from crystal import workspace as ws_mod


def current_trace_dir() -> Path:
    """The current workspace's traces/ under $MCP_EXPLORER_HOME."""
    return state.trace_dir()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Recorder:
    def __init__(self, session_id: str, source: str, trace_dir: Path | None = None, meta: dict | None = None):
        self.session_id = session_id
        self.source = source
        self.dir = trace_dir or current_trace_dir()
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

    def prompt(self, text: str, **kw) -> None:
        self._write({"ts": _now(), "session_id": self.session_id, "source": self.source, "kind": "prompt", "text": text, **kw})

    def result(self, text: str, **kw) -> None:
        self._write({"ts": _now(), "session_id": self.session_id, "source": self.source, "kind": "result", "text": text, **kw})

    def count_calls(self) -> int:
        return sum(1 for line in self.path.open() if '"kind": "call"' in line) if self.path.exists() else 0


def split_tool_name(name: str) -> tuple[str, str]:
    """Claude Code names MCP tools mcp__<server>__<tool>."""
    if name.startswith("mcp__"):
        _, server, tool = name.split("__", 2)
        return server, tool
    return "claude-code", name


CLAUDE_CODE_TOOLS = ("Read", "Grep", "Glob", "Bash")
PREVIEW_CHARS = 300


def _preview(v: Any, limit: int = PREVIEW_CHARS) -> Any:
    """Claude Code's own tool results (file contents, grep output) are context for the agent, not evidence a flow
    could reproduce: keep a short preview of every string so a trace never embeds whole files."""
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + f"… ({len(v)} chars)"
    if isinstance(v, list):
        return [_preview(x, limit) for x in v[:20]]
    if isinstance(v, dict):
        return {k: _preview(x, limit) for k, x in v.items()}
    return v


def trace_dir_for(payload: dict) -> Path:
    """The hook's `cwd` is the directory Claude Code runs in: that is the workspace, and its state dir holds the
    trace. (Created on first use, workspace.json written, seed data copied.)"""
    ws = ws_mod.workspace(payload.get("cwd") or None)
    return ws.state.ensure().traces


def _session_active(path: Path) -> bool:
    """A session is an investigation once it has made an MCP call, or was launched by `mcp-explorer record` (its
    meta names a trigger). A prompt alone does not make one: a session that only edits code leaves its prompt and
    nothing else, and Claude Code's own Read/Grep/Glob/Bash are never recorded in it."""
    if not path.exists():
        return False
    for line in path.open():
        if '"kind": "call"' in line or ('"kind": "meta"' in line and '"trigger"' in line):
            return True
    return False


def hook_main(payload: dict | None = None, trace_dir: Path | None = None) -> int:
    """Claude Code hook entry. stdin carries the hook JSON; `hook_event_name` says which hook fired:
      PostToolUse       {session_id, tool_name, tool_input, tool_response, transcript_path, cwd, ...}: every MCP call
                        is recorded; Claude Code's own Read/Grep/Glob/Bash only inside an active session (above)
      UserPromptSubmit  {session_id, prompt, cwd, ...}: the prompt is recorded (the input a later flow will take)
      Stop              {session_id, transcript_path, cwd, ...}: the agent's final message, read from the transcript
    The trace goes to the workspace the hook's `cwd` maps to. Never raises: a broken hook must not break the agent."""
    if payload is None:
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            return 0
    try:
        return _hook(payload, trace_dir)
    except Exception as e:  # noqa: BLE001
        print(f"mcp-explorer hook: {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def _hook(payload: dict, trace_dir: Path | None) -> int:
    event = payload.get("hook_event_name") or ("PostToolUse" if "tool_name" in payload else "")
    sid = payload.get("session_id", "unknown")
    tdir = trace_dir or trace_dir_for(payload)
    meta = {"transcript_path": payload.get("transcript_path"), "cwd": payload.get("cwd")}
    if event == "UserPromptSubmit":
        text = payload.get("prompt")
        if text:
            Recorder(sid, "claude-code", trace_dir=tdir, meta=meta).prompt(str(text))
        return 0
    if event == "Stop":
        if not (tdir / f"{sid}.jsonl").exists():
            return 0
        text = final_message(payload.get("transcript_path"))
        if text:
            Recorder(sid, "claude-code", trace_dir=tdir).result(text)
        return 0
    if event != "PostToolUse":
        return 0
    name = payload.get("tool_name", "")
    server, tool = split_tool_name(name)
    if server == "claude-code":
        if tool not in CLAUDE_CODE_TOOLS or not _session_active(tdir / f"{sid}.jsonl"):
            return 0  # only MCP calls and, inside an investigation, codebase reads are steps
    resp = payload.get("tool_response")
    output_text = ""
    output: Any = resp
    blocks = resp.get("content") if isinstance(resp, dict) else resp
    if isinstance(blocks, list) and blocks and all(isinstance(c, dict) for c in blocks):
        output_text = "\n".join(c.get("text", "") for c in blocks if c.get("type") == "text")
    elif isinstance(resp, str):
        output_text = resp
    if output_text:
        try:
            output = json.loads(output_text)
        except json.JSONDecodeError:
            output = output_text
    if server == "claude-code":
        output, output_text = _preview(output), _preview(output_text)
    rec = Recorder(sid, "claude-code", trace_dir=tdir, meta=meta)
    rec.seq = rec.count_calls()   # seq continues across hook invocations
    rec.record(server, tool, payload.get("tool_input") or {}, output, output_text,
               is_error=bool(isinstance(resp, dict) and resp.get("isError")),
               extra={"tool_use_id": payload.get("tool_use_id")})
    return 0


def final_message(transcript_path: str | None, limit: int = 20000) -> str | None:
    """The last assistant text in a Claude Code transcript (JSONL of {type: assistant, message: {content: [...]}})."""
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.is_file():
        return None
    last = None
    try:
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            texts = []
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            text = "\n".join(t for t in texts if t).strip()
            if text:
                last = text
    except OSError:
        return None
    return last[:limit] if last else None


if __name__ == "__main__":
    sys.exit(hook_main())
