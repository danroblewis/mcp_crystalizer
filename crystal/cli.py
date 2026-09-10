"""crystal CLI:  uv run python -m crystal.cli [--workspace <dir>] <command> ...

  --workspace <dir>             the codebase to work in (default: $CRYSTAL_WORKSPACE, else this project = the sim).
                                Supplies the code/git servers' root, the <dir>/.mcp.json layer of the server registry,
                                and the runs/<slug>/ + traces/<slug>/ namespace (the sim is the top level).
  flows                         list flows and status
  run <flow> k=v ...            run a flow with inputs; prints an evidence summary; saves runs/<id>.json
  servers                       the effective MCP servers for the workspace: transport, source file, command/url
  mcp-config [--write]          the effective mcp.json (servers.yaml < ~/.mcp.json < <workspace>/.mcp.json) for
                                Claude Code; --write stores it as <workspace>/.mcp.json
  tools [server ...]            list tools of the effective servers (a smoke test of every transport)
  induce <trigger> [--name n] [--out p] [--source s]   compile recorded traces into a draft flow
  status [--events N]           promotion lifecycle: effective state, counters, last failure per flow
  test <flow> [--live|--offline]  run the flow's regression (cassette + live fallback); records the result
  author <trigger> k=v ... --yes  run the agent (costs money): tries existing flows first, explores, induces a new version
  repair [--all | <run_id>] --yes  hand queued "this didn't help" complaints to the agent (costs money); induces new versions
  card <flow> [--write]         print the flow card (a skeleton if the YAML has none); --write appends the skeleton to the YAML
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from crystal import PROJECT_ROOT
from crystal import workspace as ws_mod
from crystal.mcp_client import ServerPool, load_registry


def cmd_serve(args):
    """serve [--port N] [--host H] [--open]: launch the web UI for the workspace and print its URL."""
    import uvicorn
    port = int(args[args.index("--port") + 1]) if "--port" in args and len(args) > args.index("--port") + 1 else 8765
    host = args[args.index("--host") + 1] if "--host" in args and len(args) > args.index("--host") + 1 else "127.0.0.1"
    ws = ws_mod.current()
    url = f"http://{host}:{port}"
    print(f"mcp-explorer: workspace {ws.name} ({ws.root})\n{url}", flush=True)
    if "--open" in args:
        import webbrowser
        webbrowser.open(url)
    uvicorn.run("crystal.app.main:app", host=host, port=port, log_level="warning")
    return 0


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


def cmd_mcp_config(args):
    """mcp-config [--write]: the effective merged config for the current workspace (what the driver hands Claude
    Code); --write stores it as <workspace>/.mcp.json, so a plain `claude` in that directory sees the same servers."""
    from crystal.registry import to_mcp_json
    ws = ws_mod.current()
    cfg = to_mcp_json(load_registry(), relative_to=ws.root)
    if "--write" in args:
        dest = ws.mcp_json()
        dest.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"wrote {dest} with {len(cfg['mcpServers'])} servers (workspace {ws})")
    else:
        print(json.dumps(cfg, indent=2))


def cmd_servers(_args):
    """servers: one line per effective server -- name, transport, the file it came from, the command or url."""
    from crystal.registry import describe
    ws = ws_mod.current()
    reg = load_registry()
    print(f"workspace {ws.slug}: {ws.root}")
    for name, spec in reg.items():
        print(describe(name, spec))
    print(f"{len(reg)} servers; runs and traces under {ws.namespaced(PROJECT_ROOT / 'runs').relative_to(PROJECT_ROOT)}/ "
          f"and {ws.namespaced(PROJECT_ROOT / 'traces').relative_to(PROJECT_ROOT)}/")


def cmd_tools(args):
    """tools [server ...]: connect to each server and list its tools; a server that fails to start is reported and
    the others still print. Exit status 1 if any failed."""
    failed = []

    async def go():
        async with ServerPool() as pool:
            for server in (args or list(pool.registry)):
                try:
                    tools = await pool.list_tools(server)
                except BaseException as e:  # noqa: BLE001  (anyio raises ExceptionGroup)
                    if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                        raise
                    failed.append(server)
                    print(f"{server}: FAILED ({pool.transport(server)}): {_explain(e)}")
                    continue
                print(f"{server}: {len(tools)} tools ({pool.transport(server)})")
                for t in tools:
                    props = (t["inputSchema"] or {}).get("properties", {})
                    desc = (t["description"] or "").strip().splitlines()
                    print(f"  {server}.{t['name']}({', '.join(props)})\n      {desc[0] if desc else ''}")
    asyncio.run(go())
    return 1 if failed else 0


def _explain(e: BaseException) -> str:
    subs = getattr(e, "exceptions", None)
    if subs:
        return "; ".join(_explain(s) for s in subs)
    return f"{type(e).__name__}: {e}"


def cmd_induce(args):
    """induce <trigger> [--name flow-name] [--out path] [--source scripted|claude-code|runner]"""
    from crystal.author import expand_flow_calls
    from crystal.induce.inducer import induce, dump_flow
    from crystal.trace.store import load_sessions
    trigger = args[0]
    name = args[args.index("--name") + 1] if "--name" in args else f"induced-{trigger.replace('_', '-')}"
    out = Path(args[args.index("--out") + 1]) if "--out" in args else PROJECT_ROOT / "flows" / f"{name}.yaml"
    source = args[args.index("--source") + 1] if "--source" in args else None
    sessions = [s for s in load_sessions(trigger=trigger) if not source or s.source == source]
    # agent sessions recorded by `crystal author`/`repair` ran existing flows through the `flows` MCP server:
    # replace each run_flow call by the calls that flow made (from its archived run record)
    sessions = [e for e in (expand_flow_calls(s)[0] for s in sessions) if e.calls]
    if not sessions:
        print("no sessions for trigger", trigger)
        return 1
    from crystal.workspace import current as _ws
    wmeta = _ws().meta()
    for sess in sessions:
        sess.meta.setdefault("workspace_meta", wmeta)   # older traces predate the workspace record
    flow, report = induce(sessions, name)
    out.write_text(dump_flow(flow))
    print(f"induced {name} from {report['sessions']} sessions -> {out}")
    print(json.dumps(report, indent=1, default=str))


def cmd_status(args):
    """status [--events N]: one row per flow with author intent, effective state, counters and last failure."""
    from crystal.flow.lifecycle import get_lifecycle, status_table
    from crystal.flow.runner import list_flows
    lc = get_lifecycle()
    rows = status_table(list_flows(), lc)
    print(f"{'flow':34} {'author':10} {'effective':10} {'streak':>6} {'clean':>5} {'fail':>4} {'tests':>7} {'cmpl':>4}  last failure")
    for r in rows:
        eff = r["status"] + ("*" if r["tripped"] else "")
        lf = (r.get("last_failure") or "")[:60]
        if r.get("last_failure_at"):
            lf = f"{r['last_failure_at'][:16]} {lf}"
        print(f"{r['name']:34} {r['author_status']:10} {eff:10} {r['clean_streak']:>6} {r['clean_runs']:>5} {r['failed_runs']:>4} "
              f"{str(r['tests_passed']) + '/' + str(r['tests_passed'] + r['tests_failed']):>7} {r['complaints']:>4}  {lf}")
        if r["hint"]:
            print(f"{'':34} {'':10} ^ {r['hint']}")
    print("* = tripped below author intent (circuit breaker); state in", lc.path)
    if "--events" in args:
        i = args.index("--events")
        n = int(args[i + 1]) if len(args) > i + 1 else 20
        print()
        for e in reversed(lc.events(limit=n)):
            tr = f" {e['from_status']} -> {e['to_status']}" if e.get("to_status") else ""
            ok = "ok" if e["ok"] else ("FAIL" if e["ok"] == 0 else "-")
            print(f"{e['ts'][:19]} {e['flow']:30} {e['kind']:10} {ok:4}{tr}  {(e.get('detail') or '')[:70]}")


def cmd_test(args):
    """test <flow> [--live | --offline]"""
    from crystal.replay.regression import regression
    if not args:
        print("usage: test <flow> [--live|--offline]")
        return 1
    mode = "live" if "--live" in args else ("offline" if "--offline" in args else "auto")
    rep = regression(args[0], mode=mode)
    for c in rep["cases"]:
        print(f"  {'PASS' if c['passed'] else 'FAIL'} inputs={c['inputs']}" + (f"  {c['reason']}" if c["reason"] else ""))
        if c.get("summary"):
            print("       " + ", ".join(f"{k}={v['hits']}" + ("!" if v.get("error") else "") for k, v in c["summary"].items()))
    if rep.get("error"):
        print("  " + rep["error"])
    lc = rep.get("lifecycle", {})
    tr = f"  transition {lc['transition'][0]} -> {lc['transition'][1]}" if lc.get("transition") else ""
    print(f"{rep['flow']}: {'PASSED' if rep['passed'] else 'FAILED'} ({mode}, cassette misses={rep['misses']})  "
          f"effective={lc.get('status')} author={lc.get('author_status')}{tr}")
    return 0 if rep["passed"] else 1


def cmd_author(args):
    from crystal.author import author_main
    return author_main(args)


def cmd_repair(args):
    from crystal.author import repair_main
    return repair_main(args)


def cmd_card(args):
    """card <flow> [--write]: the flow's card, or a deterministic skeleton when the YAML has none; --write appends
    that skeleton to the YAML (as a trailing `card:` block, so hand-written comments stay) unless a card exists."""
    from crystal.flow.cards import card_yaml, format_card, skeleton_card, validate_card
    from crystal.flow.runner import load_flow
    if not args or args[0].startswith("--"):
        print("usage: card <flow> [--write]")
        return 1
    flow = load_flow(args[0])
    card = flow.get("card")
    if card:
        problems = validate_card(flow, card)
        print(format_card(card, flow))
        for p in problems:
            print("problem:", p)
        if "--write" in args:
            print(f"{flow['_path']} already has a card (authored_by: {card.get('authored_by')}); nothing written")
        return 1 if problems else 0
    card = skeleton_card(flow)
    print(format_card(card, flow))
    if "--write" in args:
        p = Path(flow["_path"])
        text = p.read_text()
        p.write_text(text + ("" if text.endswith("\n") else "\n") + "\n" + card_yaml(card))
        print(f"wrote skeleton card to {p}")
    else:
        print("(skeleton; not in the YAML. `card <flow> --write` stores it)")
    return 0


COMMANDS = {"serve": cmd_serve, "flows": cmd_flows, "induce": cmd_induce, "run": cmd_run, "mcp-config": cmd_mcp_config, "tools": cmd_tools,
            "servers": cmd_servers, "status": cmd_status, "test": cmd_test, "author": cmd_author, "repair": cmd_repair,
            "card": cmd_card}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ws, argv = ws_mod.split_argv(argv)
    if ws is not None:
        try:
            ws_mod.activate(ws)   # $CRYSTAL_WORKSPACE for this process and every child (servers, Claude Code, its hook)
        except FileNotFoundError as e:
            print(e)
            return 1
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not argv or argv[0].startswith("--"):
        argv = ["serve", *argv]          # `mcp-explorer` alone serves the current directory
    if argv[0] not in COMMANDS:
        print(f"unknown command {argv[0]!r}\n" + __doc__)
        return 1
    return COMMANDS[argv[0]](argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
