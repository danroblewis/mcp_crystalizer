"""Agent-assisted authoring and repair (milestone 2). The only code paths that launch the agent, and only on an
explicit command with --yes (or an interactive confirmation). Every launch costs real money; the cost is printed.

  mcp-explorer author <trigger> k=v ... [--yes] [--budget 3] [--model m] [--name base] [--no-test]
      Runs Claude Code headless with a prompt that lists the existing flows for the trigger and says: run them FIRST
      through the `flows` MCP server (run_flow), explore with the raw tools only for what the flow lacked, then
      summarise. The trace shows "ran flow X, then did Y". The run_flow call is expanded into the calls the flow made
      (from the saved run record) and the inducer compiles that session plus the existing sessions of the trigger
      into <state dir>/flows/<base>.v<N>.yaml (a new version; promoted flows are never overwritten).

  mcp-explorer repair [--all | <run_id>] [--yes] [--budget 3] [--model m] [--no-test]
      Consumes the workspace's feedback.jsonl (the UI's "this didn't help" queue): for each unhelpful run the agent gets the
      flow YAML, the inputs, a compact evidence summary and the complaint, finds what was missing with the MCP tools,
      and a new version is induced as above. The complaint is marked handled by appending a record (never deleted).

Both commands accept a `driver_fn` when used as a library so the plumbing is testable without the agent.
"""
from __future__ import annotations


import json
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

from crystal import state
from crystal.flow.cards import card_prompt, merge_card, parse_card, skeleton_card
from crystal.flow.runner import list_flows, load_flow
from crystal.induce.inducer import STEP_NAMES, dump_flow, induce
from crystal.trace.driver import PROMPTS, run_agent
from crystal.trace.store import Session, load_session, load_sessions


def feedback_path() -> Path:
    """The repair queue: one per workspace (complaints name their run and flow)."""
    return state.feedback_path()


def _rdir(run_dir: Path | None) -> Path:
    return run_dir or state.run_dir()


def _tdir(trace_dir: Path | None) -> Path:
    return trace_dir or state.trace_dir()


RAW_SERVERS = "jira, slack, confluence, chronosphere, logz, pagerduty, git, code"
VERSION_RX = re.compile(r"^(.*)\.v(\d+)$")


# ---------------------------------------------------------------- flows and prompts
def flows_for_trigger(trigger: str, flow_dir: Path | None = None, lifecycle=None) -> list[dict]:
    """Flows for a trigger, most trusted first (the agent is told to run flows[0]); versions of one base after their
    base. Trust is the runtime's effective status (circuit breaker), not the YAML's author intent, unless
    lifecycle=False; `effective_status` is set on each flow."""
    flows = list_flows() if flow_dir is None else _list_flows(flow_dir)
    flows = [f for f in flows if f.get("status") != "broken" and (f.get("trigger") or {}).get("type") == trigger]
    if lifecycle is not False:
        from crystal.flow.lifecycle import get_lifecycle
        lc = lifecycle or get_lifecycle()
        for f in flows:
            f["effective_status"] = lc.view(f)["status"]
    rank = {"promoted": 0, "candidate": 1, "draft": 2}
    return sorted(flows, key=lambda f: (rank.get(f.get("effective_status", f.get("status")), 3), base_name(f["name"]), -int(f.get("version") or 1)))


def _list_flows(flow_dir: Path) -> list[dict]:
    out = []
    for p in sorted(flow_dir.glob("*.yaml")):
        f = yaml.safe_load(p.read_text())
        f["_path"] = str(p)
        out.append(f)
    return out


def describe_flow(f: dict) -> str:
    steps = " -> ".join(f"{s['id']}({s.get('tool')})" for s in f.get("steps", []))
    ins = ", ".join(f"{k}: {v.get('type')}" for k, v in (f.get("inputs") or {}).items())
    eff = f.get("effective_status")
    status = f"{f.get('status')}" if eff in (None, f.get("status")) else f"{f.get('status')}, tripped to {eff}"
    return f"- {f['name']} [{status}] inputs({ins}); steps: {steps}"


def author_prompt(trigger: str, inputs: dict, flows: list[dict]) -> str:
    goal = PROMPTS.get(trigger, "Investigate {inputs} using the MCP tools available. Finish with a short summary.")
    try:
        goal = goal.format(**inputs, inputs=json.dumps(inputs))
    except KeyError:
        goal = goal.replace("{inputs}", json.dumps(inputs))
    lines = [goal, ""]
    if flows:
        lines += ["Crystallized flows already exist for this kind of investigation; the `flows` MCP server runs them without AI:"]
        lines += [describe_flow(f) for f in flows]
        best = flows[0]["name"]
        lines += ["",
                  f"Step 1 (do this FIRST, before any other tool): call mcp__flows__run_flow with name=\"{best}\" and "
                  f"inputs_json='{json.dumps(inputs)}' and read the evidence it returns. If it errors, try the other listed flows.",
                  f"Step 2: use the raw MCP tools ({RAW_SERVERS}) ONLY to find what the flow's evidence lacked or got wrong "
                  "(steps with zero hits, errors, or items from the goal above that no step covers). Do not repeat calls the flow already made.",
                  "Step 3: finish with a short summary in three parts: what the flow found, what you had to add by hand "
                  "(which tools and queries), and what is still missing."]
    else:
        lines += [f"No crystallized flow exists yet for trigger {trigger}. Use the raw MCP tools ({RAW_SERVERS}) directly.",
                  "Finish with a short summary of what you found and which calls produced it."]
    lines += ["Use the tools directly; do not ask questions.", "", card_request(flows[0] if flows else _stub_flow(trigger, inputs))]
    return "\n".join(lines)


def _stub_flow(trigger: str, inputs: dict) -> dict:
    """What the induced flow will look like when no flow exists yet: inputs from the command, no steps."""
    return {"name": f"induced-{trigger.replace('_', '-')}", "title": f"induced {trigger.replace('_', ' ')}",
            "trigger": {"type": trigger}, "inputs": {k: {"type": "string", "required": True, "example": v} for k, v in inputs.items()}, "steps": []}


def card_request(flow: dict) -> str:
    """The flow-card instructions for the end of the agent's message, with the skeleton of `flow` as the draft and the
    step names the inducer will give hand-added calls."""
    names = ", ".join(f"{tool} -> {sid}" for tool, sid in STEP_NAMES.items())
    return card_prompt(flow, skeleton_card(flow)) + f"\nThe inducer names steps after their tool ({names}; other tools keep their tool name)."


def compact_evidence(record: dict | None, max_chars: int = 3500) -> str:
    """One line per step: hits, error, extracted values; fan-out items listed briefly. For prompts."""
    if not record:
        return "(run record not available)"
    lines = [f"run {record.get('run_id')} status={record.get('status')} inputs={json.dumps(record.get('inputs'))}"]
    for s in record.get("steps", []):
        if s.get("skipped"):
            lines.append(f"- {s['id']}: skipped ({s['skipped']})")
            continue
        ex = {k: (v if not isinstance(v, list) else v[:5]) for k, v in (s.get("extracts") or {}).items()}
        line = f"- {s['id']} {s.get('tool')}: hits={s.get('hits')}" + (f" ERROR {str(s['error'])[:120]}" if s.get("error") else "")
        if "items" in s:
            line += " items=" + ", ".join(f"{str(it.get('item'))[:40]}({it.get('hits')})" for it in s["items"][:6])
        elif s.get("args"):
            line += f" args={json.dumps(s['args'], default=str)[:200]}"
        if ex:
            line += f" extracts={json.dumps(ex, default=str)[:300]}"
        lines.append(line)
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n…(truncated)"


def repair_prompt(complaint: dict, flow: dict | None, flow_yaml: str, record: dict | None) -> str:
    inputs = complaint.get("inputs") or (record or {}).get("inputs") or {}
    name = complaint.get("flow") or (flow or {}).get("name") or "?"
    return "\n".join([
        f"A user ran the crystallized flow `{name}` with inputs {json.dumps(inputs)} and said it did not help.",
        f"Their complaint: \"{(complaint.get('text') or '').strip() or '(no text given)'}\"",
        "", "The flow (YAML):", "```yaml", flow_yaml.strip()[:6000], "```", "",
        "What the run produced (per step: hits, errors, extracted values):", compact_evidence(record), "",
        "Your job: find what the run was missing, using the MCP tools.",
        f"Step 1 (FIRST): call mcp__flows__run_flow with name=\"{name}\" and inputs_json='{json.dumps(inputs)}' to see the current evidence.",
        f"Step 2: use the raw MCP tools ({RAW_SERVERS}) to obtain what the complaint asks for and whatever the flow's evidence lacked. "
        "Do not repeat calls the flow already made unless a different query is needed.",
        "Step 3: finish with a short summary: what was missing, which tool calls (with their arguments) produce it, and how the "
        "values in those arguments derive from earlier results.",
        "Use the tools directly; do not ask questions.",
        "", card_request(flow or _stub_flow((flow or {}).get("trigger", {}).get("type") or "unknown", inputs)),
    ])


# ---------------------------------------------------------------- traces: expand run_flow into the flow's own calls
# run records referenced by recorded agent sessions are archived under <traces>/runs/, next to the trace


def _run_id_of(output) -> str | None:
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return None
    return output.get("run_id") if isinstance(output, dict) else None


def _record_of(output, run_dir: Path | None = None, trace_dir: Path | None = None) -> dict | None:
    run_id = _run_id_of(output)
    if not run_id:
        return None
    for d in (_rdir(run_dir), _tdir(trace_dir) / "runs"):
        p = d / f"{run_id}.json"
        if p.exists():
            return json.loads(p.read_text())
    return None


def archive_run_records(session: Session, run_dir: Path | None = None, trace_dir: Path | None = None) -> list[Path]:
    """Copy the run records a session's run_flow calls point at from runs/ (gitignored) into traces/runs/, next to
    the trace, so re-inducing the session later still expands run_flow into the flow's own calls."""
    dest = _tdir(trace_dir) / "runs"
    copied = []
    for c in session.calls:
        if c.get("server") != "flows" or c.get("tool") != "run_flow":
            continue
        run_id = _run_id_of(c.get("output"))
        src = _rdir(run_dir) / f"{run_id}.json" if run_id else None
        if src and src.exists() and not (dest / src.name).exists():
            dest.mkdir(parents=True, exist_ok=True)
            (dest / src.name).write_text(src.read_text())
            copied.append(dest / src.name)
    return copied


def flow_calls(record: dict) -> list[dict]:
    """The calls a saved flow run made, in trace-record shape. Ladder rungs that missed are re-created as empty
    results so the inducer sees the ladder; fan-out items become one call each."""
    calls = []

    def add(tool, args, result, error):
        server, name = tool.split(".", 1)
        calls.append({"server": server, "tool": name, "input": args, "output": result if result is not None else {},
                      "output_text": json.dumps(result, default=str) if result is not None else "", "is_error": bool(error),
                      "from_flow": record.get("flow")})

    for s in record.get("steps", []):
        if s.get("skipped") or not s.get("tool"):
            continue
        subs = s["items"] if "items" in s else [s]
        for sub in subs:
            if not sub.get("args"):
                continue
            attempts = [a for a in sub.get("attempts") or [] if not a.get("skipped")]
            if len(attempts) > 1:
                key = next((k for k, v in sub["args"].items() if v == attempts[-1].get("value")), None)
                for a in attempts[:-1]:
                    if a.get("values"):                      # several laddered args per attempt
                        add(s["tool"], {**sub["args"], **a["values"]}, {}, a.get("error"))
                    elif key is not None:
                        add(s["tool"], {**sub["args"], key: a["value"]}, {}, a.get("error"))
            add(s["tool"], sub["args"], sub.get("result"), sub.get("error"))
    return calls


def expand_flow_calls(session: Session, run_dir: Path | None = None, trace_dir: Path | None = None) -> tuple[Session, list[str]]:
    """Replace each flows.run_flow call by the calls that flow made; drop flows.list_flows and Claude Code's own
    Read/Grep/Glob (a flow cannot run those). Returns the new session and the names of the flows it ran. A run_flow
    call whose run record is missing from both runs/ and traces/runs/ (or that returned an error, so no run was
    saved) is dropped with a warning on stderr: the session then merges as its raw calls only."""
    calls, ran = [], []
    for c in session.calls:
        if c["server"] == "flows":
            if c["tool"] == "run_flow":
                rec = _record_of(c.get("output"), run_dir, trace_dir)
                name = (c.get("input") or {}).get("name", "?")
                if rec:
                    ran.append(rec["flow"])
                    calls.extend(flow_calls(rec))
                else:
                    ran.append(name)
                    why = "run_flow returned an error" if _run_id_of(c.get("output")) is None else f"run record {_run_id_of(c.get('output'))}.json missing from runs/ and traces/runs/"
                    print(f"warning: session {session.session_id}: run_flow({name}) call #{c.get('seq')} dropped ({why}); "
                          "the session contributes its raw calls only", file=sys.stderr)
            continue
        if c["server"] == "claude-code":
            continue
        calls.append(c)
    for i, c in enumerate(calls, 1):
        c["seq"] = i
    return replace(session, calls=calls), ran


# ---------------------------------------------------------------- versions
def base_name(name: str) -> str:
    m = VERSION_RX.match(name)
    return m.group(1) if m else name


def next_version(base: str, flow_dir: Path | None = None) -> tuple[Path, int]:
    d = flow_dir or state.flow_dir()
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    p0 = d / f"{base}.yaml"
    if p0.exists():
        try:
            n = int((yaml.safe_load(p0.read_text()) or {}).get("version") or 1)
        except Exception:  # noqa: BLE001
            n = 1
    for p in d.glob(f"{base}.v*.yaml"):
        m = VERSION_RX.match(p.stem)
        if m:
            n = max(n, int(m.group(2)))
    n += 1
    while (d / f"{base}.v{n}.yaml").exists():   # never overwrite anything
        n += 1
    return d / f"{base}.v{n}.yaml", n


def induce_version(trigger: str, base: str, new_session: Session, trace_dir: Path | None = None, flow_dir: Path | None = None,
                   catalog: dict | None = None, note: dict | None = None, run_dir: Path | None = None,
                   agent_text: str | None = None) -> dict:
    """Induce <base>.v<N> from the new session plus every existing session of the trigger (earlier agent sessions
    get their run_flow calls expanded too). Writes the file. `agent_text` is the agent's final message: the flow card
    it ends with (a fenced yaml block) is merged over the inducer's skeleton card, its step ids checked against the
    new flow; without a parseable card the skeleton stays, with a warning in the report."""
    others = [expand_flow_calls(s, run_dir, trace_dir)[0] for s in load_sessions(trace_dir, trigger=trigger) if s.session_id != new_session.session_id]
    sessions = [s for s in others if s.calls] + [new_session]
    out, n = next_version(base, flow_dir)
    flow, report = induce(sessions, f"{base}.v{n}", catalog=catalog)
    flow["version"] = n
    flow["base"] = base
    flow["status"] = "draft"
    flow["authored"] = {"at": datetime.now(timezone.utc).isoformat(), "session": new_session.session_id, **(note or {})}
    if agent_text is not None:
        flow["card"], warnings = merge_card(flow, parse_card(agent_text), flow.get("card"))
        report["card"] = {"authored_by": flow["card"]["authored_by"], "warnings": warnings}
        for w in warnings:
            print(f"warning: card for {flow['name']}: {w}", file=sys.stderr)
    # steps that only this session contributed are the diff the agent added
    added = [s["id"] for s in flow["steps"] if s.get("optional") and s.get("seen_in", "").startswith("1/")]
    out.write_text(dump_flow(flow))
    report["added_steps"] = added
    report["path"] = str(out)
    report["name"] = flow["name"]
    return {"path": out, "flow": flow, "report": report, "sessions": len(sessions)}


def print_induction(res: dict) -> None:
    r = res["report"]
    print(f"induced {r['name']} from {r['sessions']} sessions ({r['steps']} steps) -> {r['path']}")
    if r.get("added_steps"):
        print("  steps added by this session:", ", ".join(r["added_steps"]))
    if r.get("optional_steps"):
        print("  optional:", ", ".join(r["optional_steps"]))
    if r.get("unresolved"):
        print("  unresolved:", json.dumps(r["unresolved"])[:400])
    if r.get("ladders"):
        print("  ladders:", json.dumps(r["ladders"]))
    if r.get("cut_refs"):
        print("  reference cycles cut:", json.dumps(r["cut_refs"]))
    if r.get("dropped_rungs"):
        print("  dropped literal rungs (one session's value, unexplained):", json.dumps(r["dropped_rungs"]))
    if r.get("tests"):
        print(f"  tests: {r['tests']['cases']} cases; min_hits on {', '.join(r['tests']['min_hits'])}")
    if r.get("forEach"):
        print("  forEach:", json.dumps(r["forEach"]))
    if r.get("card"):
        print(f"  card: authored_by {r['card']['authored_by']}" + (f"; warnings: {'; '.join(r['card']['warnings'])}" if r["card"]["warnings"] else ""))


# ---------------------------------------------------------------- feedback queue
def load_feedback(path: Path | None = None) -> list[dict]:
    p = path or feedback_path()
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def pending_complaints(path: Path | None = None) -> list[dict]:
    recs = load_feedback(path)
    handled = {r.get("run_id") for r in recs if r.get("kind") == "handled"}
    return [r for r in recs if r.get("kind", "feedback") == "feedback" and r.get("helpful") is False and r.get("run_id") not in handled]


def mark_handled(run_id: str, info: dict, path: Path | None = None) -> None:
    p = path or feedback_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "kind": "handled", "run_id": run_id, **info}) + "\n")


# ---------------------------------------------------------------- confirmation
def confirm(args: list[str], what: str) -> bool:
    if "--yes" in args or "-y" in args:
        return True
    if sys.stdin.isatty():
        ans = input(f"{what}\nThis launches Claude Code and costs real money. Proceed? [y/N] ").strip().lower()
        return ans in ("y", "yes")
    print("refusing to launch the agent without --yes (non-interactive)")
    return False


def _opt(args: list[str], flag: str, default=None):
    return args[args.index(flag) + 1] if flag in args and len(args) > args.index(flag) + 1 else default


VALUE_OPTS = ("--budget", "--model", "--name")


def positionals(args: list[str]) -> list[str]:
    """Arguments that are neither options nor an option's value (`--budget 5` is not the run id 5)."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in VALUE_OPTS:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


# ---------------------------------------------------------------- commands
def _ran_flow_first(session: Session) -> bool:
    """Was the first MCP call run_flow? Claude Code's own Read/Grep/Glob (recorded by the hook) do not count."""
    mcp = [c for c in session.calls if c["server"] != "claude-code"]
    return bool(mcp) and mcp[0]["server"] == "flows" and mcp[0]["tool"] == "run_flow"


def _no_calls_error(info: dict) -> str:
    msg = "the session recorded no tool calls; nothing to induce"
    if info.get("is_error"):
        msg += f" (the agent run failed: {(info.get('result') or info.get('stderr') or '')[:200].strip() or 'is_error'})"
    return msg


def author(trigger: str, inputs: dict, budget="3", model=None, name: str | None = None, driver_fn=run_agent,
           trace_dir: Path | None = None, flow_dir: Path | None = None, run_dir: Path | None = None, test: bool = True,
           catalog: dict | None = None, lifecycle=None) -> dict:
    flows = flows_for_trigger(trigger, flow_dir, lifecycle)
    prompt = author_prompt(trigger, inputs, flows)
    info = driver_fn(trigger, inputs, prompt, budget=budget, model=model, meta={"command": "author"})
    session = load_session(Path(info["trace_path"]))
    archive_run_records(session, run_dir, trace_dir)
    expanded, ran = expand_flow_calls(session, run_dir, trace_dir)
    base = base_name(name or (ran[0] if ran and ran[0] != "?" else (flows[0]["name"] if flows else f"induced-{trigger.replace('_', '-')}")))
    res = {"agent": info, "ran_flows": ran, "ran_flow_first": _ran_flow_first(session),
           "raw_calls": [f"{c['server']}.{c['tool']}" for c in session.calls if c["server"] not in ("flows", "claude-code")]}
    if not expanded.calls:
        res["error"] = _no_calls_error(info)
        return res
    ind = induce_version(trigger, base, expanded, trace_dir, flow_dir, catalog, run_dir=run_dir,
                         note={"command": "author", "inputs": inputs, "cost_usd": info.get("cost_usd"), "ran_flows": ran},
                         agent_text=info.get("result") or "")
    res.update(ind)
    if test:
        res["test"] = _test_new(ind["flow"])
    return res


def repair(selector: str, budget="3", model=None, driver_fn=run_agent, feedback_path: Path | None = None,
           trace_dir: Path | None = None, flow_dir: Path | None = None, run_dir: Path | None = None, test: bool = True,
           catalog: dict | None = None) -> list[dict]:
    todo = pending_complaints(feedback_path)
    if selector != "--all":
        todo = [c for c in todo if c.get("run_id") == selector]
    results = []
    for c in todo:
        p = _rdir(run_dir) / f"{c['run_id']}.json"
        record = json.loads(p.read_text()) if p.exists() else None
        fname = c.get("flow") or (record or {}).get("flow")
        try:
            flow = load_flow(fname) if flow_dir is None else load_flow(flow_dir / f"{fname}.yaml")
            flow_yaml = Path(flow["_path"]).read_text()
        except Exception:  # noqa: BLE001
            flow, flow_yaml = None, f"(flow {fname} not found)"
        trigger = (flow or {}).get("trigger", {}).get("type") or "unknown"
        inputs = c.get("inputs") or (record or {}).get("inputs") or {}
        prompt = repair_prompt(c, flow, flow_yaml, record)
        info = driver_fn(trigger, inputs, prompt, budget=budget, model=model, meta={"command": "repair", "complaint_run": c["run_id"], "complaint": c.get("text")})
        session = load_session(Path(info["trace_path"]))
        archive_run_records(session, run_dir, trace_dir)
        expanded, ran = expand_flow_calls(session, run_dir, trace_dir)
        res = {"complaint": c, "agent": info, "ran_flows": ran}
        if expanded.calls:
            ind = induce_version(trigger, base_name(fname or f"induced-{trigger}"), expanded, trace_dir, flow_dir, catalog, run_dir=run_dir,
                                 note={"command": "repair", "complaint_run": c["run_id"], "complaint": c.get("text"), "cost_usd": info.get("cost_usd")},
                                 agent_text=info.get("result") or "")
            res.update(ind)
            if test:
                res["test"] = _test_new(ind["flow"])
            # handled only once a version exists; a failed agent run (error, budget, no calls) leaves the complaint pending
            mark_handled(c["run_id"], {"session_id": info["session_id"], "cost_usd": info.get("cost_usd"), "new_flow": ind["flow"]["name"]}, feedback_path)
        else:
            res["error"] = _no_calls_error(info) + "; the complaint stays pending"
        results.append(res)
    return results


def _test_new(flow: dict) -> dict:
    """Regression on the freshly induced version (auto mode: cassette seeded from its sessions, live for misses)."""
    from crystal.replay.regression import regression
    try:
        return regression(flow, mode="auto")
    except Exception as e:  # noqa: BLE001
        return {"passed": False, "error": str(e), "cases": []}


def _print_agent(info: dict) -> None:
    print(f"agent session {info['session_id']}: turns={info.get('num_turns')} error={info.get('is_error')} calls={len(info.get('calls', []))}")
    for c in info.get("calls", []):
        print(f"  {c['seq']:2} {c['server']}.{c['tool']} {json.dumps(c.get('input'), default=str)[:100]}")
    print("--- agent summary ---\n" + (info.get("result") or "")[:2500] + "\n---")
    print(f"COST: ${info.get('cost_usd') or 0:.4f}")


def _print_test(t: dict | None) -> None:
    if not t:
        return
    for c in t.get("cases", []):
        print(f"  test {'PASS' if c['passed'] else 'FAIL'} {c['inputs']}" + (f" {c['reason']}" if c.get("reason") else ""))
    print(f"  regression: {'PASSED' if t.get('passed') else 'FAILED'}" + (f" ({t['error']})" if t.get("error") else ""))


def author_main(args: list[str]) -> int:
    if not args or args[0].startswith("--"):
        print("usage: author <trigger> k=v ... [--yes] [--budget 3] [--model m] [--name base] [--no-test]")
        return 1
    trigger = args[0]
    inputs = dict(a.split("=", 1) for a in args[1:] if "=" in a and not a.startswith("--"))
    budget = _opt(args, "--budget", "3")
    flows = flows_for_trigger(trigger)
    print(f"trigger {trigger} inputs {inputs}; existing flows: {[f['name'] for f in flows] or 'none'}; budget ${budget}")
    if not confirm(args, f"mcp-explorer author {trigger} {inputs}"):
        return 2
    res = author(trigger, inputs, budget=budget, model=_opt(args, "--model"), name=_opt(args, "--name"), test="--no-test" not in args)
    _print_agent(res["agent"])
    print(f"ran flow first: {res['ran_flow_first']}; flows run: {res['ran_flows']}; raw tool calls: {len(res['raw_calls'])}")
    if res.get("error"):
        print("error:", res["error"])
        return 1
    print_induction(res)
    _print_test(res.get("test"))
    return 0


def repair_main(args: list[str]) -> int:
    pending = pending_complaints()
    sel = "--all" if "--all" in args else next(iter(positionals(args)), None)
    if not sel:
        print(f"{len(pending)} pending complaint(s) in {feedback_path()}:")
        for c in pending:
            print(f"  {c['run_id']}  {c.get('flow')}  {c.get('inputs')}  \"{(c.get('text') or '')[:80]}\"")
        print("usage: repair [--all | <run_id>] [--yes] [--budget 3] [--model m] [--no-test]")
        return 0 if not pending else 1
    todo = pending if sel == "--all" else [c for c in pending if c.get("run_id") == sel]
    if not todo:
        print("no pending complaint matches", sel)
        return 1
    budget = _opt(args, "--budget", "3")
    print(f"{len(todo)} complaint(s) to repair, budget ${budget} each:")
    for c in todo:
        print(f"  {c['run_id']}  {c.get('flow')}  {c.get('inputs')}  \"{(c.get('text') or '')[:80]}\"")
    if not confirm(args, f"mcp-explorer repair {sel}"):
        return 2
    total = 0.0
    for res in repair(sel, budget=budget, model=_opt(args, "--model"), test="--no-test" not in args):
        print(f"\n== complaint {res['complaint']['run_id']} ({res['complaint'].get('flow')}): \"{res['complaint'].get('text')}\"")
        _print_agent(res["agent"])
        total += res["agent"].get("cost_usd") or 0
        if res.get("error"):
            print("error:", res["error"], "(not marked handled)")
            continue
        print_induction(res)
        _print_test(res.get("test"))
        print("marked handled in", feedback_path())
    print(f"\nTOTAL COST: ${total:.4f}")
    return 0
