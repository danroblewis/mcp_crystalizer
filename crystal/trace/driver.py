"""Run the real agent (Claude Code headless) against the effective MCP servers with trace recording.

  uv run python -m crystal.trace.driver [--workspace <dir>] jira_issue key=SUP-105 [--model sonnet] [--budget 3] [--prompt-file f]
  uv run python -m crystal.trace.driver --workspace workspaces/agentarena codebase "question=where is the match loop?"

Claude Code runs in the workspace (its cwd, so Read/Grep/Glob work there like any agent's) with `--mcp-config` set
to the effective registry for that workspace (servers.yaml < ~/.mcp.json < <workspace>/.mcp.json; written to a
temporary mcp.json so the agent sees exactly the servers the flow runner uses) and `--strict-mcp-config`, so nothing
else it is configured with leaks in. Recording: the session id is chosen up front so the trace file is known; a
meta record (trigger, inputs, workspace) is written before launch, and the project's PostToolUse hook
(.claude/settings.json, passed with --settings when the workspace is not the project) appends every MCP call to the
same file under traces/<workspace-slug>/.

`run_agent` is the library entry point used by `crystal author` / `crystal repair`; nothing in this codebase calls it
without an explicit user command, and every call costs real money (cap with budget).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from crystal import PROJECT_ROOT
from crystal import workspace as ws_mod
from crystal.mcp_client import load_registry
from crystal.registry import to_mcp_json
from crystal.trace.record import TRACE_DIR, Recorder
from crystal.trace.store import load_session

SETTINGS = PROJECT_ROOT / ".claude" / "settings.json"

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
    # a real codebase (any workspace): answer a question about it with whatever servers the workspace has
    "codebase": (
        "You are an engineer new to this repository. Answer this question about it: \"{question}\". Use the MCP tools "
        "available (code, git, and any others such as github or deepwiki) to find the relevant files, the history behind "
        "them and who owns them. Use the tools directly; do not ask questions. Finish with a short summary citing files."
    ),
}


def build_command(prompt: str, sid: str, budget, model: str | None, servers: list[str], mcp_config: Path,
                  ws: ws_mod.Workspace) -> list[str]:
    allowed = [f"mcp__{s}" for s in servers] + [f"mcp__{s}__*" for s in servers] + ["Read", "Grep", "Glob"]
    cmd = ["claude", "-p", prompt, "--session-id", sid, "--output-format", "json", "--max-budget-usd", str(budget),
           "--allowedTools", *allowed, "--mcp-config", str(mcp_config), "--strict-mcp-config"]
    if not ws.is_project:
        # the recording hook lives in the project's settings; running in another directory, Claude Code would not
        # load them on its own
        cmd += ["--settings", str(SETTINGS)]
    if model:
        cmd += ["--model", model]
    return cmd


def agent_env(ws: ws_mod.Workspace) -> dict[str, str]:
    """The child's environment: no CLAUDE_* (nested-session detection), the workspace for the servers it spawns
    and for the hook (which records under traces/<slug>/), and the project dir the hook must `cd` into."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env[ws_mod.ENV] = str(ws.root)
    env["CRYSTAL_PROJECT_DIR"] = str(PROJECT_ROOT)
    return env


def run_agent(trigger: str, inputs: dict, prompt: str, budget: str | float = "3", model: str | None = None,
              servers: list[str] | None = None, meta: dict | None = None, session_id: str | None = None,
              quiet: bool = False, workspace: str | Path | None = None) -> dict:
    """Launch `claude -p` once with recording, in the workspace (`workspace`, else $CRYSTAL_WORKSPACE, else the
    project). Returns {session_id, cost_usd, num_turns, duration_ms, is_error, result, returncode, stderr, trace_path,
    calls, workspace, mcp_config}. COSTS MONEY: capped by `budget` (USD)."""
    ws = ws_mod.workspace(workspace)
    sid = session_id or str(uuid.uuid4())
    trace_dir = ws.namespaced(TRACE_DIR)
    Recorder(sid, "claude-code", trace_dir=trace_dir,
             meta={"trigger": trigger, "inputs": inputs, "model": model or "default", "prompt": prompt,
                   "workspace": ws.slug, "workspace_root": str(ws.root), **(meta or {})})
    registry = load_registry(workspace=ws)
    servers = servers or list(registry)
    cfg = to_mcp_json(registry, only=servers)
    tmp = tempfile.NamedTemporaryFile("w", prefix="crystal-mcp-", suffix=".json", delete=False)
    with tmp:
        json.dump(cfg, tmp, indent=2)
    mcp_config = Path(tmp.name)
    cmd = build_command(prompt, sid, budget, model, servers, mcp_config, ws)
    if not quiet:
        print(f"session {sid} in workspace {ws}\n$ {' '.join(cmd[:4])} ... ({len(cfg['mcpServers'])} servers from "
              f"{mcp_config.name}, budget ${budget})", flush=True)
    try:
        proc = subprocess.run(cmd, cwd=ws.root, env=agent_env(ws), capture_output=True, text=True)
    finally:
        mcp_config.unlink(missing_ok=True)
    out = proc.stdout.strip()
    res: dict = {}
    try:
        res = json.loads(out)
    except json.JSONDecodeError:
        res = {"result": out[:3000], "is_error": True}
    info = {"session_id": sid, "cost_usd": res.get("total_cost_usd"), "num_turns": res.get("num_turns"), "duration_ms": res.get("duration_ms"),
            "is_error": bool(res.get("is_error")) or proc.returncode != 0, "result": str(res.get("result", "")), "returncode": proc.returncode,
            "stderr": proc.stderr[-2000:], "trace_path": str(trace_dir / f"{sid}.jsonl"), "calls": [],
            "workspace": ws.slug, "mcp_config": cfg}
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
    ws, argv = ws_mod.split_argv(argv)
    if ws is not None:
        ws_mod.activate(ws)
    if not argv:
        print(__doc__)
        return 1
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
