"""Run the real agent (Claude Code headless) against the registered MCP servers with trace recording.

  uv run python -m crystal.trace.driver jira_issue key=SUP-105 [--model sonnet] [--budget 3] [--prompt-file f]

The session id is chosen up front so the trace file is known; a meta record (trigger, inputs) is written
before launch, and the project's PostToolUse hook appends every MCP call to the same file.

`run_agent` is the library entry point used by `crystal author` / `crystal repair`; nothing in this codebase calls it
without an explicit user command, and every call costs real money (cap with budget).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from crystal import PROJECT_ROOT
from crystal.mcp_client import load_registry
from crystal.trace.record import Recorder
from crystal.trace.store import load_session

PROMPTS = {
    "jira_issue": (
        "You are the on-call engineer. Investigate Jira ticket {key} using the MCP tools available "
        "(jira, slack, confluence, chronosphere, logz, pagerduty, git, code). Find: the Slack discussion about it, "
        "the code that raises or logs the error, the owning team, the runbook, the error-rate metric around the "
        "incident, the log lines for every trace id you come across, commits to the service shortly before, and the "
        "PagerDuty incident. Use the tools directly; do not ask questions. Finish with a short summary of what you found."
    ),
    "slack_thread": (
        "You were added to a Slack thread: channel {channel_id}, thread {thread_ts}. Read it with the slack tools, then "
        "investigate what it is about using jira, confluence, chronosphere, logz, git, code and pagerduty as needed. "
        "Use the tools directly; do not ask questions. Finish with a short summary."
    ),
    "slack_dm": (
        "Someone DM'd you on Slack: \"{text}\". Investigate using the MCP tools available (slack, jira, confluence, "
        "chronosphere, logz, pagerduty, git, code). Use the tools directly; do not ask questions. Finish with a short summary."
    ),
}


def run_agent(trigger: str, inputs: dict, prompt: str, budget: str | float = "3", model: str | None = None,
              servers: list[str] | None = None, meta: dict | None = None, session_id: str | None = None,
              quiet: bool = False) -> dict:
    """Launch `claude -p` once with recording. Returns {session_id, cost_usd, num_turns, duration_ms, is_error, result,
    returncode, stderr, trace_path, calls}. COSTS MONEY: capped by `budget` (USD)."""
    sid = session_id or str(uuid.uuid4())
    Recorder(sid, "claude-code", meta={"trigger": trigger, "inputs": inputs, "model": model or "default", "prompt": prompt, **(meta or {})})
    servers = servers or list(load_registry())
    allowed = [f"mcp__{s}" for s in servers] + [f"mcp__{s}__*" for s in servers] + ["Read", "Grep", "Glob"]
    cmd = ["claude", "-p", prompt, "--session-id", sid, "--output-format", "json", "--max-budget-usd", str(budget),
           "--allowedTools", *allowed, "--mcp-config", str(PROJECT_ROOT / ".mcp.json")]
    if model:
        cmd += ["--model", model]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}  # avoid nested-session detection
    if not quiet:
        print(f"session {sid}\n$ {' '.join(cmd[:4])} ... ({len(allowed)} allowed tools, budget ${budget})", flush=True)
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    out = proc.stdout.strip()
    res: dict = {}
    try:
        res = json.loads(out)
    except json.JSONDecodeError:
        res = {"result": out[:3000], "is_error": True}
    info = {"session_id": sid, "cost_usd": res.get("total_cost_usd"), "num_turns": res.get("num_turns"), "duration_ms": res.get("duration_ms"),
            "is_error": bool(res.get("is_error")) or proc.returncode != 0, "result": str(res.get("result", "")), "returncode": proc.returncode,
            "stderr": proc.stderr[-2000:], "trace_path": str(PROJECT_ROOT / "traces" / f"{sid}.jsonl"), "calls": []}
    p = Path(info["trace_path"])
    if p.exists():
        info["calls"] = [{"seq": c["seq"], "server": c["server"], "tool": c["tool"], "input": c.get("input")} for c in load_session(p).calls]
    return info


def print_result(info: dict) -> None:
    print("result:", json.dumps({k: info.get(k) for k in ("is_error", "num_turns", "duration_ms", "cost_usd", "session_id")}))
    print("\n--- agent's final message ---\n" + info.get("result", "")[:3000])
    if info.get("returncode"):
        print("stderr:", info.get("stderr", ""))
    print(f"\nrecorded {len(info['calls'])} calls -> {Path(info['trace_path']).name}")
    for c in info["calls"]:
        print(f"  {c['seq']:2} {c['server']}.{c['tool']} {json.dumps(c['input'])[:100]}")
    print(f"cost: ${info.get('cost_usd') or 0:.4f}")


def main(argv: list[str]) -> int:
    trigger = argv[0]
    inputs = dict(a.split("=", 1) for a in argv[1:] if "=" in a and not a.startswith("--"))
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    budget = argv[argv.index("--budget") + 1] if "--budget" in argv else "3"
    prompt = Path(argv[argv.index("--prompt-file") + 1]).read_text() if "--prompt-file" in argv else PROMPTS[trigger]
    prompt = prompt.format(**inputs)
    info = run_agent(trigger, inputs, prompt, budget=budget, model=model)
    print_result(info)
    return info["returncode"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
