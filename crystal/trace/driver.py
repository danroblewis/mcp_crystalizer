"""Run the real agent (Claude Code headless) against the registered MCP servers with trace recording.

  uv run python -m crystal.trace.driver jira_issue key=SUP-105 [--model sonnet] [--budget 3] [--prompt-file f]

The session id is chosen up front so the trace file is known; a meta record (trigger, inputs) is written
before launch, and the project's PostToolUse hook appends every MCP call to the same file.
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


def main(argv: list[str]) -> int:
    trigger = argv[0]
    inputs = dict(a.split("=", 1) for a in argv[1:] if "=" in a and not a.startswith("--"))
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    budget = argv[argv.index("--budget") + 1] if "--budget" in argv else "3"
    prompt = Path(argv[argv.index("--prompt-file") + 1]).read_text() if "--prompt-file" in argv else PROMPTS[trigger]
    prompt = prompt.format(**inputs)
    sid = str(uuid.uuid4())
    Recorder(sid, "claude-code", meta={"trigger": trigger, "inputs": inputs, "model": model or "default", "prompt": prompt})
    servers = list(load_registry())
    allowed = [f"mcp__{s}" for s in servers] + [f"mcp__{s}__*" for s in servers] + ["Read", "Grep", "Glob"]
    cmd = ["claude", "-p", prompt, "--session-id", sid, "--output-format", "json", "--max-budget-usd", budget,
           "--allowedTools", *allowed, "--mcp-config", str(PROJECT_ROOT / ".mcp.json")]
    if model:
        cmd += ["--model", model]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}  # avoid nested-session detection
    print(f"session {sid}\n$ {' '.join(cmd[:4])} ... ({len(allowed)} allowed tools)", flush=True)
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    out = proc.stdout.strip()
    try:
        res = json.loads(out)
        summary = {k: res.get(k) for k in ("is_error", "num_turns", "duration_ms", "total_cost_usd", "session_id")}
        print("result:", json.dumps(summary))
        print("\n--- agent's final message ---\n" + str(res.get("result", ""))[:3000])
    except json.JSONDecodeError:
        print("stdout:", out[:2000])
    if proc.returncode != 0:
        print("stderr:", proc.stderr[-2000:])
    path = PROJECT_ROOT / "traces" / f"{sid}.jsonl"
    if path.exists():
        s = load_session(path)
        print(f"\nrecorded {len(s.calls)} calls -> {path.name}")
        for c in s.calls:
            print(f"  {c['seq']:2} {c['server']}.{c['tool']} {json.dumps(c['input'])[:100]}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
