"""mcp-explorer: open the current directory as a workspace, serve the web UI, run and crystallize flows.

  mcp-explorer [--workspace <dir>] [<command> ...]        no command = serve

  --workspace <dir>             the directory to work in (default: $CRYSTAL_WORKSPACE, else the current directory).
                                Supplies the code/git servers' root, its .mcp.json, and its state dir under
                                $MCP_EXPLORER_HOME (default ~/.mcp-explorer/workspaces/<slug>/).
  serve [--port N] [--open]     launch the web UI for the workspace and print its URL (the default command)
  flows                         list the workspace's flows and their status
  run <flow> k=v ...            run a flow with inputs; prints an evidence summary; saves the run
  test <flow> [--live|--offline]  run the flow's regression (cassette + live fallback); records the result
  status [--events N]           promotion lifecycle: effective state, counters, last failure per flow
  card <flow> [--write]         print the flow card (a skeleton if the YAML has none); --write appends the skeleton
  servers                       the effective MCP servers for the workspace: transport, source, command/url
  tools [server ...]            connect to the effective servers and list their tools (a smoke test)
  mcp-config [--write]          the effective mcp.json (built-ins < ~/.claude.json < ~/.mcp.json < <workspace>/.mcp.json);
                                --write stores it as <workspace>/.mcp.json so a plain `claude` here sees the same servers
  induce <trigger> [--name n] [--out p] [--source s]   compile recorded traces into a draft flow (no AI)
  record <trigger> k=v ... --yes  run Claude Code headless here with recording (COSTS MONEY; --budget caps it)
  author <trigger> k=v ... --yes  run the agent (costs money): tries existing flows first, explores, induces a new version
  repair [--all | <run_id>] --yes  hand queued "this didn't help" complaints to the agent (costs money)
  hook                          the Claude Code hook entry (reads the hook JSON on stdin); not for humans
  install-hook [--uninstall] [--settings p] [--status]   add the recording hooks to ~/.claude/settings.json
  seed --from <dir> [--overwrite]  copy <dir>/flows, traces, catalog.yaml into the workspace's state dir
  workspaces                    list the workspaces this tool has state for
  import [--all] [--transcripts DIR] [--dry-run] [--force] [--reattribute] [--verbose]
                                import past Claude Code sessions (~/.claude/projects transcripts) as traces, for free:
                                only sessions that made MCP calls; default = this workspace's, --all = every project;
                                a session the hook already recorded gets its subagent calls attributed instead
                                (--reattribute redoes that join)
  candidates [--top N] [--min-support N] [--json]   recurring tool sequences across this workspace's episodes
  candidates induce <n> --name <flow> [--out p]     compile candidate <n> into a draft flow (no AI)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from crystal import state as state_mod
from crystal import workspace as ws_mod
from crystal.mcp_client import ServerPool, load_registry


def cmd_serve(args):
    """serve [--port N] [--host H] [--open]: launch the web UI for the workspace and print its URL."""
    import uvicorn
    port = int(args[args.index("--port") + 1]) if "--port" in args and len(args) > args.index("--port") + 1 else 8765
    host = args[args.index("--host") + 1] if "--host" in args and len(args) > args.index("--host") + 1 else "127.0.0.1"
    ws = ws_mod.current()
    st = ws.state.ensure()
    url = f"http://{host}:{port}"
    print(f"mcp-explorer: workspace {ws.name} ({ws.root})\nstate: {st}\n{url}", flush=True)
    if "--open" in args:
        import webbrowser
        webbrowser.open(url)
    uvicorn.run("crystal.app.main:app", host=host, port=port, log_level="warning")
    return 0


def cmd_flows(_args):
    from crystal.flow.runner import list_flows
    flows = list_flows()
    if not flows:
        print(f"no flows yet in {state_mod.current().flows}\n(install the hook, run Claude Code here or `mcp-explorer record`, then `mcp-explorer induce <trigger>`)")
        return 0
    for f in flows:
        print(f"{f.get('name'):32} {f.get('status', '?'):10} inputs={list((f.get('inputs') or {}).keys())}  {f.get('title', '')}")


def cmd_run(args):
    from crystal.flow.runner import run_flow_sync
    from crystal.trace.record import Recorder
    if not args:
        print("usage: run <flow> k=v ...")
        return 1
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
    print(f"saved {state_mod.current().runs / (record['run_id'] + '.json')}")


def cmd_mcp_config(args):
    """mcp-config [--write]: the effective merged config for the current workspace (what `record` hands Claude
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
    """servers: one line per effective server -- name, transport, where it came from, the command or url."""
    from crystal.registry import describe
    ws = ws_mod.current()
    reg = load_registry()
    print(f"workspace {ws.name}: {ws.root}")
    for name, spec in reg.items():
        print(describe(name, spec, ws))
    print(f"{len(reg)} servers; state in {ws.state}")


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
    if not args or args[0].startswith("--"):
        print("usage: induce <trigger> [--name flow-name] [--out path] [--source scripted|claude-code|runner]")
        return 1
    trigger = args[0]
    name = args[args.index("--name") + 1] if "--name" in args else f"induced-{trigger.replace('_', '-')}"
    out = Path(args[args.index("--out") + 1]) if "--out" in args else state_mod.current().flows / f"{name}.yaml"
    source = args[args.index("--source") + 1] if "--source" in args else None
    sessions = [s for s in load_sessions(trigger=trigger) if not source or s.source == source]
    # agent sessions recorded by `author`/`repair` ran existing flows through the `flows` MCP server:
    # replace each run_flow call by the calls that flow made (from its archived run record)
    sessions = [e for e in (expand_flow_calls(s)[0] for s in sessions) if e.calls]
    if not sessions:
        print(f"no sessions for trigger {trigger} in {state_mod.current().traces}")
        return 1
    wmeta = ws_mod.current().meta()
    for sess in sessions:
        sess.meta.setdefault("workspace_meta", wmeta)   # older traces predate the workspace record
    flow, report = induce(sessions, name)
    out.parent.mkdir(parents=True, exist_ok=True)
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


def cmd_record(args):
    from crystal.trace.driver import main as driver_main
    return driver_main(args)


def cmd_hook(_args):
    """The Claude Code hook: reads the hook JSON on stdin (crystal/trace/record.py)."""
    from crystal.trace.record import hook_main
    return hook_main()


def cmd_install_hook(args):
    from crystal.hooks import main as hooks_main
    return hooks_main(args)


def cmd_seed(args):
    """seed --from <dir> [--overwrite]: copy <dir>/flows/*.yaml, <dir>/traces/**, <dir>/catalog.yaml into the
    workspace's state dir (existing files kept unless --overwrite)."""
    if "--from" not in args or len(args) <= args.index("--from") + 1:
        print("usage: seed --from <dir> [--overwrite]")
        return 1
    src = Path(args[args.index("--from") + 1]).expanduser().resolve()
    st = ws_mod.current().state.ensure()
    counts = state_mod.seed(st, src, overwrite="--overwrite" in args)
    print(f"seeded {st} from {src}: {counts['flows']} flows, {counts['traces']} trace files, catalog={'yes' if counts['catalog'] else 'kept/none'}")
    return 0


def cmd_workspaces(_args):
    rows = state_mod.list_workspaces()
    if not rows:
        print(f"no workspaces yet under {state_mod.home()}")
        return 0
    print(f"{'workspace':24} {'flows':>5} {'runs':>5} {'traces':>6}  root  (state under {state_mod.home()})")
    for r in rows:
        gone = "" if r.get("exists", True) else "  (directory missing)"
        print(f"{r.get('name', '?'):24} {r['flows']:>5} {r['runs']:>5} {r['traces']:>6}  {r.get('root', '?')}{gone}")
    return 0


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


def _opt(args: list[str], flag: str, default=None):
    if flag in args and len(args) > args.index(flag) + 1:
        return args[args.index(flag) + 1]
    return default


def cmd_import(args):
    """import [--all] [--transcripts DIR] [--dry-run] [--force] [--reattribute] [--verbose]: read past Claude Code
    session transcripts and write the ones with MCP calls into the workspace's trace dir (crystal/trace/transcripts.py).
    Default: transcripts whose cwd is this workspace; --all: every project, each into its own workspace."""
    from crystal.trace.transcripts import format_table, import_transcripts, transcripts_dir
    base = transcripts_dir(_opt(args, "--transcripts"))
    if not base.is_dir():
        print(f"no transcripts directory at {base} (Claude Code keeps them under ~/.claude/projects; --transcripts <dir> overrides)")
        return 1
    ws = ws_mod.current()
    rep = import_transcripts(base, workspace_root=ws.root, all_projects="--all" in args, dry_run="--dry-run" in args,
                             force="--force" in args, reattribute_again="--reattribute" in args)
    print(format_table(rep, verbose="--verbose" in args))
    if not rep.get("dry_run") and (rep["imported"] or rep.get("reattributed")):
        print(f"traces written under {state_mod.home() / 'workspaces'}; next: `mcp-explorer candidates`")
    return 0


def cmd_candidates(args):
    """candidates [--top N] [--min-support N] [--json] | candidates induce <n> --name <flow> [--out p]"""
    from crystal.induce.mining import candidates, format_candidates
    min_support = int(_opt(args, "--min-support", 2))
    if args and args[0] == "induce":
        rest = args[1:]
        if not rest or not rest[0].isdigit():
            print("usage: candidates induce <n> --name <flow> [--out path]")
            return 1
        n = int(rest[0])
        cands = candidates(state_mod.current().traces, min_support=min_support)
        if n < 1 or n > len(cands):
            print(f"no candidate #{n} ({len(cands)} candidates; `mcp-explorer candidates` lists them)")
            return 1
        cand = cands[n - 1]
        name = _opt(rest, "--name") or f"mined-{cand.servers[0]}-{cand.servers[-1]}-{n}"
        out = Path(_opt(rest, "--out")) if "--out" in rest else state_mod.current().flows / f"{name}.yaml"
        from crystal.induce.mining import induce_candidate
        from crystal.induce.inducer import dump_flow
        flow, report = induce_candidate(cand, name, workspace_meta=ws_mod.current().meta())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dump_flow(flow))
        print(f"induced {name} from candidate #{n} ({cand.support} episodes, {cand.length} steps) -> {out}")
        print(json.dumps({k: v for k, v in report.items() if k in ("sessions", "steps", "unresolved", "optional_steps", "ladders", "forEach", "tests")}, indent=1, default=str))
        return 0
    top = int(_opt(args, "--top", 20))
    cands = candidates(state_mod.current().traces, min_support=min_support, limit=top)
    if "--json" in args:
        print(json.dumps([c.view() for c in cands], indent=1))
        return 0
    print(format_candidates(cands))
    if cands:
        print("\n`mcp-explorer candidates induce <#> --name <flow>` compiles one into a draft flow (no AI).")
    return 0


COMMANDS = {"serve": cmd_serve, "flows": cmd_flows, "run": cmd_run, "test": cmd_test, "status": cmd_status, "card": cmd_card,
            "servers": cmd_servers, "tools": cmd_tools, "mcp-config": cmd_mcp_config, "induce": cmd_induce,
            "record": cmd_record, "author": cmd_author, "repair": cmd_repair, "hook": cmd_hook,
            "install-hook": cmd_install_hook, "seed": cmd_seed, "workspaces": cmd_workspaces,
            "import": cmd_import, "candidates": cmd_candidates}
NO_WORKSPACE = {"hook", "install-hook", "workspaces"}     # commands that do not act on the current workspace


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ws, argv = ws_mod.split_argv(argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not argv or argv[0].startswith("--"):
        argv = ["serve", *argv]          # `mcp-explorer` alone serves the current directory
    if argv[0] not in COMMANDS:
        print(f"unknown command {argv[0]!r}\n" + __doc__)
        return 1
    if argv[0] not in NO_WORKSPACE:
        try:
            ws_mod.activate(ws)   # $CRYSTAL_WORKSPACE for this process and every child (servers, Claude Code, its hook)
        except FileNotFoundError as e:
            print(e)
            return 1
        ws_mod.current().state.ensure()   # first sight: workspace.json, seed data from <workspace>/.mcp-explorer/
    return COMMANDS[argv[0]](argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
