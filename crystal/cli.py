"""crystal CLI:  uv run python -m crystal.cli <command> ...

  flows                         list flows and status
  run <flow> k=v ...            run a flow with inputs; prints an evidence summary; saves runs/<id>.json
  mcp-config                    write .mcp.json for Claude Code from servers.yaml
  tools [server]                list tools of registered servers
  induce <trigger> [--name n] [--out p] [--source s]   compile recorded traces into a draft flow
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from crystal import PROJECT_ROOT
from crystal.mcp_client import ServerPool, load_registry


def cmd_flows(_args):
    from crystal.flow.runner import list_flows
    for f in list_flows():
        print(f"{f.get('name'):32} {f.get('status', '?'):10} inputs={list((f.get('inputs') or {}).keys())}  {f.get('title', '')}")


def cmd_run(args):
    from crystal.flow.runner import run_flow_sync
    from crystal.trace.record import Recorder
    name, kv = args[0], args[1:]
    inputs = dict(a.split("=", 1) for a in kv if "=" in a)
    rec = None
    if "--record" in kv:
        import uuid
        rec = Recorder("run-" + uuid.uuid4().hex[:8], "runner", meta={"trigger": name, "inputs": inputs})
    record = run_flow_sync(name, inputs, recorder=rec)
    print_summary(record)


def print_summary(record: dict) -> None:
    print(f"run {record['run_id']}  flow={record['flow']}  status={record['status']}  inputs={record['inputs']}")
    for s in record["steps"]:
        if s.get("skipped"):
            print(f"  - {s['id']:16} skipped ({s['skipped']})")
            continue
        if "items" in s:
            print(f"  - {s['id']:16} {s['tool']:40} items={len(s['items'])} hits={s.get('hits')}" + (f"  ERROR {s['error']}" if s.get("error") else ""))
            for it in s["items"]:
                ex = {k: (v if not isinstance(v, list) else f"[{len(v)}]") for k, v in it["extracts"].items()}
                print(f"      · {str(it['item'])[:44]:44} hits={it['hits']} {ex if ex else ''}")
        else:
            rung = ""
            if s.get("attempts"):
                used = [a for a in s["attempts"] if a.get("hits")]
                rung = f" rung={used[0]['rung']}/{len(s['attempts'])}" if used else f" rungs-tried={len(s['attempts'])}"
            ex = {k: (v if not isinstance(v, (list, dict)) else (f"[{len(v)}]" if isinstance(v, list) else v.get("start", "{..}"))) for k, v in s.get("extracts", {}).items()}
            print(f"  - {s['id']:16} {s['tool']:40} hits={s.get('hits')}{rung}" + (f"  ERROR {s['error']}" if s.get("error") else ""))
            if ex:
                print(f"      {json.dumps(ex, default=str)[:160]}")
    print(f"saved runs/{record['run_id']}.json")


def cmd_mcp_config(_args):
    reg = load_registry()
    cfg = {"mcpServers": {name: {"command": spec["command"], "args": spec.get("args", []), **({"env": spec["env"]} if spec.get("env") else {})}
                          for name, spec in reg.items()}}
    (PROJECT_ROOT / ".mcp.json").write_text(json.dumps(cfg, indent=2))
    print(f"wrote .mcp.json with {len(cfg['mcpServers'])} servers")


def cmd_tools(args):
    async def go():
        async with ServerPool() as pool:
            for server in (args or list(pool.registry)):
                for t in await pool.list_tools(server):
                    props = (t["inputSchema"] or {}).get("properties", {})
                    print(f"{server}.{t['name']}({', '.join(props)})\n    {t['description']}")
    asyncio.run(go())


def cmd_induce(args):
    """induce <trigger> [--name flow-name] [--out path] [--source scripted|claude-code|runner]"""
    from crystal.induce.inducer import induce, dump_flow
    from crystal.trace.store import load_sessions
    trigger = args[0]
    name = args[args.index("--name") + 1] if "--name" in args else f"induced-{trigger.replace('_', '-')}"
    out = Path(args[args.index("--out") + 1]) if "--out" in args else PROJECT_ROOT / "flows" / f"{name}.yaml"
    source = args[args.index("--source") + 1] if "--source" in args else None
    sessions = [s for s in load_sessions(trigger=trigger) if not source or s.source == source]
    if not sessions:
        print("no sessions for trigger", trigger)
        return 1
    flow, report = induce(sessions, name)
    out.write_text(dump_flow(flow))
    print(f"induced {name} from {report['sessions']} sessions -> {out}")
    print(json.dumps(report, indent=1, default=str))


COMMANDS = {"flows": cmd_flows, "induce": cmd_induce, "run": cmd_run, "mcp-config": cmd_mcp_config, "tools": cmd_tools}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 1
    return COMMANDS[argv[0]](argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
