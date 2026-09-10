"""Flow cards: the one-screen description of a flow that the catalog and the dossier show, and the acceptance
criteria a run is judged against. Nothing here calls an LLM.

A card is an optional top-level `card:` mapping in the flow YAML:

    card:
      use_case: str                       # when a person reaches for this flow, 1-2 sentences
      inputs_explained: {input_name: str}
      expected_outputs:                   # ordered; one per evidence group; step ids must exist in the flow
        - {name: str, steps: [step_id, ...], description: str, required: bool}
      not_covered: [str, ...]
      example: {inputs: {..}, found: str} # one sentence of what a real run found
      authored_by: skeleton | agent | human   # skeleton = deterministic draft; agent = LLM prose; human = hand-written

Three entry points:
  skeleton_card(flow, report)  a deterministic draft from the steps' tools, extracts and fan-outs (the inducer
                               attaches one to every draft; `crystal card <flow>` prints one for flows without a card)
  coverage(flow, run)          "found N of M expected things" for a run record, naming the gaps
  headline(flow, run)          the dossier facts (what, when, service, owners, status, what changed, repeats, people,
                               trace ids, pods) derived from the run's extracts and typed results only
plus parse_card / merge_card, used by `crystal author` to take the card the agent writes at the end of its message.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

SERVERS = ("jira", "slack", "confluence", "chronosphere", "logz", "pagerduty", "git", "code")
CARD_KEYS = ("use_case", "inputs_explained", "expected_outputs", "not_covered", "example", "authored_by")
AUTHORED_BY = ("skeleton", "agent", "human")

# tool -> (evidence group, what the group's results are). Steps of one group become one expected output.
GROUPS: dict[str, tuple[str, str]] = {
    "jira.jira_get_issue": ("ticket", "the Jira ticket"),
    "jira.jira_search": ("related tickets", "Jira tickets found by search"),
    "jira.jira_get_issue_comments": ("ticket comments", "the comments on the ticket"),
    "slack.conversations_search_messages": ("slack discussion", "Slack messages found by search"),
    "slack.conversations_replies": ("thread", "the replies of the Slack thread"),
    "slack.conversations_history": ("conversation", "the channel or DM history"),
    "code.grep": ("code", "source lines that raise or log the error"),
    "code.read_file": ("code", "the source file that handles the request"),
    "code.codeowners": ("owning team", "CODEOWNERS entries for the matching files"),
    "confluence.confluence_search": ("runbook", "Confluence pages found by search"),
    "confluence.confluence_get_page": ("runbook", "the runbook page body"),
    "chronosphere.query_prometheus_range": ("metrics", "a metric series over the window"),
    "chronosphere.query_prometheus": ("metrics", "a metric value"),
    "logz.search_logs": ("logs", "log lines"),
    "git.git_log": ("commits", "commits touching the service just before"),
    "git.git_show": ("suspect commit", "the diff of a commit named in the evidence"),
    "pagerduty.list_incidents": ("pagerduty incident", "PagerDuty incidents on the service in the window"),
    "pagerduty.get_incident": ("pagerduty incident", "the PagerDuty incident"),
    "pagerduty.list_services": ("pagerduty services", "the PagerDuty service list"),
}


# ---------------------------------------------------------------- skeleton
def group_of(step: dict) -> str:
    """Evidence group of a step: by id hint first (a `repeats` jira_search is not the ticket search), then by tool."""
    sid = str(step.get("id", ""))
    if "repeat" in sid or "recurr" in sid:
        return "repeats"
    tool = step.get("tool") or ""
    if tool in GROUPS:
        return GROUPS[tool][0]
    return tool.split(".", 1)[0] or sid


def _describe_step(step: dict) -> str:
    tool = step.get("tool") or "?"
    what = GROUPS.get(tool, (None, tool))[1]
    bits = [f"{what} ({tool}"]
    if step.get("forEach"):
        bits[0] += f", per item of `{step['forEach']}`"
        if step.get("max_items"):
            bits[0] += f", first {step['max_items']}"
    ladders = [(k, len(v["ladder"])) for k, v in (step.get("args") or {}).items() if isinstance(v, dict) and "ladder" in v]
    for k, n in ladders:
        bits[0] += f", {n}-rung ladder on {k}"
    bits[0] += ")"
    if step.get("when") is not None:
        bits.append(f"only when `{step['when']}`")
    ex = list((step.get("extract") or {}).keys())
    if ex:
        bits.append("extracts " + ", ".join(ex))
    return "; ".join(bits)


def _required_ids(flow: dict, report: dict | None) -> set[str]:
    ids_ = {s["id"] for s in flow.get("steps") or [] if s.get("required")}
    ids_ |= set(((report or {}).get("tests") or {}).get("min_hits") or [])
    for case in flow.get("tests") or []:
        ids_ |= {k for k, v in (case.get("expect") or {}).items() if (v or {}).get("min_hits")}
    return ids_


def skeleton_card(flow: dict, report: dict | None = None) -> dict:
    """A deterministic card drafted from the flow alone (plus the inducer's report when there is one): one expected
    output per evidence group, descriptions from tools/extracts/fan-outs, `required` from `required:` steps and the
    steps that had hits in every traced session. No LLM."""
    steps = flow.get("steps") or []
    groups: dict[str, list[dict]] = {}
    for st in steps:
        groups.setdefault(group_of(st), []).append(st)
    required = _required_ids(flow, report)
    expected = []
    for name, members in groups.items():
        expected.append({"name": name, "steps": [s["id"] for s in members],
                         "description": "; ".join(_describe_step(s) for s in members),
                         "required": any(s["id"] in required for s in members)})
    inputs = flow.get("inputs") or {}
    inputs_explained = {}
    for k, spec in inputs.items():
        spec = spec or {}
        txt = f"{spec.get('label') or k} ({spec.get('type') or 'string'})"
        if spec.get("example") not in (None, ""):
            txt += f", e.g. {spec['example']}"
        inputs_explained[k] = txt
    trigger = (flow.get("trigger") or {}).get("type") or "unknown"
    title = (flow.get("title") or flow.get("name") or "this flow").strip()
    use_case = f"{title}. Run it on a {trigger.replace('_', ' ')} ({', '.join(inputs) or 'no inputs'}) to gather: {', '.join(groups) or 'nothing yet'}."
    used = {(s.get("tool") or "").split(".", 1)[0] for s in steps}
    not_covered = [f"no {srv} lookups" for srv in SERVERS if srv not in used]
    for st in steps:
        if st.get("forEach") and st.get("max_items"):
            not_covered.append(f"{st['id']} looks at the first {st['max_items']} items of its fan-out only")
    tests = flow.get("tests") or []
    ex_inputs = dict(tests[0].get("inputs") or {}) if tests else {k: (v or {}).get("example") for k, v in inputs.items() if (v or {}).get("example") not in (None, "")}
    min_hits = sorted(_required_ids(flow, report) - {s["id"] for s in steps if s.get("required")})
    found = f"Every traced run had hits on: {', '.join(min_hits)}." if min_hits else ""
    return {"use_case": use_case, "inputs_explained": inputs_explained, "expected_outputs": expected,
            "not_covered": not_covered, "example": {"inputs": ex_inputs, "found": found}, "authored_by": "skeleton"}


# ---------------------------------------------------------------- coverage
def step_hits(run: dict, sid: str) -> int:
    """Hits of a step in a run record: fan-out steps sum their items; skipped or absent steps count 0."""
    for s in run.get("steps") or []:
        if s.get("id") != sid:
            continue
        if s.get("skipped"):
            return 0
        if "items" in s:
            return sum(int(it.get("hits") or 0) for it in s["items"])
        return int(s.get("hits") or 0)
    return 0


def coverage(flow: dict, run: dict) -> dict:
    """{"expected": [{name, steps, description, required, found, hits}], "found": n, "total": m, "missing": [names],
    "required_missing": [names]}. An expected output is found when any of its steps has hits. A flow without a card
    is judged against its skeleton card."""
    card = flow.get("card") or skeleton_card(flow)
    out = []
    for eo in card.get("expected_outputs") or []:
        steps = list(eo.get("steps") or [])
        hits = sum(step_hits(run, sid) for sid in steps)
        out.append({"name": eo.get("name"), "steps": steps, "description": eo.get("description", ""),
                    "required": bool(eo.get("required")), "found": hits > 0, "hits": hits})
    return {"expected": out, "found": sum(1 for e in out if e["found"]), "total": len(out),
            "missing": [e["name"] for e in out if not e["found"]],
            "required_missing": [e["name"] for e in out if e["required"] and not e["found"]]}


# ---------------------------------------------------------------- headline
def _merged_extracts(step: dict) -> dict:
    """A fan-out step's extracts merged across items the way the runner's context does (lists, deduplicated)."""
    if "items" not in step:
        return dict(step.get("extracts") or {})
    merged: dict[str, list] = {}
    for it in step["items"]:
        for k, v in (it.get("extracts") or {}).items():
            merged.setdefault(k, [])
            for x in (v if isinstance(v, list) else [v]):
                if x is not None and x not in merged[k]:
                    merged[k].append(x)
    return merged


def _live_steps(run: dict) -> list[dict]:
    return [s for s in run.get("steps") or [] if not s.get("skipped")]


def _results(step: dict) -> list[Any]:
    if "items" in step:
        return [it.get("result") for it in step["items"] if it.get("result") is not None]
    return [step["result"]] if step.get("result") is not None else []


def _first(exs: list[tuple[str, dict]], *keys: str):
    for _, ex in exs:
        for k in keys:
            v = ex.get(k)
            if isinstance(v, list):
                v = next((x for x in v if x not in (None, "")), None)
            if v not in (None, "", {}):
                return v
    return None


def _union(exs: list[tuple[str, dict]], *keys: str) -> list:
    out: list = []
    for _, ex in exs:
        for k in keys:
            v = ex.get(k)
            for x in (v if isinstance(v, list) else [v]):
                if x not in (None, "") and x not in out:
                    out.append(x)
    return out


def _get(d: Any, path: str):
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _window_of(exs: list[tuple[str, dict]]) -> dict | None:
    for prefer in (("window",), ()):
        for _, ex in exs:
            for k, v in ex.items():
                if prefer and k not in prefer:
                    continue
                if isinstance(v, dict) and v.get("start") and v.get("end"):
                    return {kk: v[kk] for kk in ("anchor", "start", "end") if v.get(kk)}
    return None


def headline(flow: dict, run: dict) -> dict:
    """The dossier facts, from extracts and typed results only; every key is present only when known:
    what, when, service, team_owners, status, changed_before ({commits, suspect_sha}), repeat_of, people, trace_ids,
    pods, ticket."""
    steps = _live_steps(run)
    exs = [(s["id"], _merged_extracts(s)) for s in steps]
    by_tool: dict[str, list[dict]] = {}
    for s in steps:
        by_tool.setdefault(s.get("tool") or "", []).append(s)
    h: dict[str, Any] = {}

    issues = [r for s in by_tool.get("jira.jira_get_issue", []) for r in _results(s) if isinstance(r, dict) and r.get("key")]
    own_keys = {str(v) for v in (run.get("inputs") or {}).values() if isinstance(v, str) and re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", v)}
    own_keys |= {r["key"] for r in issues}
    if own_keys:
        h["ticket"] = sorted(own_keys)[0] if len(own_keys) == 1 else sorted(own_keys)

    error_sig = _first(exs, "error_sig", "error")
    error_class = _first(exs, "error_class")
    what = error_sig or error_class or _first(exs, "summary") or (issues[0]["fields"].get("summary") if issues and isinstance(issues[0].get("fields"), dict) else None)
    if what:
        h["what"] = what
    if error_class:
        h["error_class"] = error_class

    win = _window_of(exs)
    anchor = _first(exs, "created", "time")
    if win or anchor:
        h["when"] = {**({"anchor": anchor} if anchor else {}), **(win or {})}

    service = _first(exs, "service", "service_in_text")
    if service:
        h["service"] = service

    teams = _union(exs, "teams")
    if not teams:
        for s in by_tool.get("pagerduty.list_incidents", []) + by_tool.get("pagerduty.get_incident", []):
            for r in _results(s):
                for inc in (r.get("incidents") if isinstance(r, dict) and isinstance(r.get("incidents"), list) else [r.get("incident") if isinstance(r, dict) else None]):
                    for t in (inc or {}).get("teams") or []:
                        name = t.get("summary") if isinstance(t, dict) else t
                        if name and name not in teams:
                            teams.append(name)
    if teams:
        h["team_owners"] = teams

    status: dict[str, Any] = {}
    jira_status = _first(exs, "status") or next((_get(r, "fields.status.name") for r in issues if _get(r, "fields.status.name")), None)
    if jira_status:
        status["jira"] = jira_status
    pd = []
    for s in by_tool.get("pagerduty.list_incidents", []):
        for r in _results(s):
            for inc in (r.get("incidents") or []) if isinstance(r, dict) else []:
                if isinstance(inc, dict) and inc.get("status"):
                    pd.append(inc["status"])
    for s in by_tool.get("pagerduty.get_incident", []):
        for r in _results(s):
            st = _get(r, "incident.status") or (r.get("status") if isinstance(r, dict) else None)
            if st:
                pd.append(st)
    if pd:
        status["pagerduty"] = pd[0] if len(set(pd)) == 1 else pd
    if status:
        h["status"] = status

    commits = []
    for s in by_tool.get("git.git_log", []):
        for r in _results(s):
            for c in (r.get("commits") or []) if isinstance(r, dict) else []:
                if isinstance(c, dict) and c.get("sha") and c["sha"] not in {x["sha"] for x in commits}:
                    commits.append({k: c.get(k) for k in ("sha", "subject", "author", "date")})
    suspect = None
    shorts = _union(exs, "shas", "sha_shorts", "sha")
    for sh in shorts:
        full = next((c["sha"] for c in commits if c["sha"].startswith(str(sh))), None)
        if full:
            suspect = full
            break
    if suspect is None:
        for s in by_tool.get("git.git_show", []):
            for r in _results(s):
                m = re.match(r"commit ([0-9a-f]{7,40})", r if isinstance(r, str) else str(r.get("raw", "")) if isinstance(r, dict) else "")
                if m:
                    suspect = m.group(1)
                    break
            if suspect:
                break
    if commits or suspect:
        h["changed_before"] = {**({"commits": commits} if commits else {}), **({"suspect_sha": suspect} if suspect else {})}

    repeats = []
    for s in by_tool.get("jira.jira_search", []):
        for r in _results(s):
            for iss in (r.get("issues") or []) if isinstance(r, dict) else []:
                if not isinstance(iss, dict) or not iss.get("key") or iss["key"] in own_keys:
                    continue
                f = iss.get("fields") or {}
                text = f"{f.get('summary', '')} {f.get('description', '')}"
                comps = [c.get("name") if isinstance(c, dict) else c for c in f.get("components") or []]
                if error_class and error_class not in text:
                    continue
                if service and comps and service not in comps:
                    continue
                if iss["key"] not in {x["key"] for x in repeats}:
                    repeats.append({"key": iss["key"], "summary": f.get("summary"), "status": _get(f, "status.name"), "created": f.get("created")})
    if repeats:
        h["repeat_of"] = repeats

    people = _union(exs, "people", "sender")
    if people:
        h["people"] = people
    trace_ids = _union(exs, "trace_ids", "trace_id")
    if trace_ids:
        h["trace_ids"] = trace_ids
    pods = _union(exs, "pods")
    if pods:
        h["pods"] = pods
    return h


# ---------------------------------------------------------------- the agent's card: parse, validate, merge
FENCE_RX = re.compile(r"```ya?ml[^\n]*\n(.*?)```", re.S)


def parse_card(text: str | None) -> dict | None:
    """The card from an agent's final message: the last fenced ```yaml block that parses to a mapping carrying
    `card:` or any card key. None when there is no such block."""
    if not text:
        return None
    for block in reversed(FENCE_RX.findall(text)):
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("card"), dict):
            data = data["card"]
        if isinstance(data, dict) and any(k in data for k in CARD_KEYS):
            return data
    return None


def validate_card(flow: dict, card: dict) -> list[str]:
    """Problems with a card against a flow: unknown step ids, malformed expected outputs, unknown inputs."""
    ids_ = {s["id"] for s in flow.get("steps") or []}
    problems = []
    eos = card.get("expected_outputs")
    if eos is not None and not isinstance(eos, list):
        problems.append("expected_outputs is not a list")
        eos = []
    for i, eo in enumerate(eos or []):
        if not isinstance(eo, dict) or not eo.get("name"):
            problems.append(f"expected_outputs[{i}] has no name")
            continue
        steps = eo.get("steps")
        if not isinstance(steps, list) or not steps:
            problems.append(f"expected output {eo['name']!r} names no steps")
            continue
        for sid in steps:
            if sid not in ids_:
                problems.append(f"expected output {eo['name']!r} names unknown step {sid!r}")
    for k in (card.get("inputs_explained") or {}) if isinstance(card.get("inputs_explained"), dict) else []:
        if k not in (flow.get("inputs") or {}):
            problems.append(f"inputs_explained names unknown input {k!r}")
    if card.get("authored_by") not in (None, *AUTHORED_BY):
        problems.append(f"authored_by must be one of {', '.join(AUTHORED_BY)}")
    return problems


def merge_card(flow: dict, parsed: dict | None, skeleton: dict | None = None, authored_by: str = "agent") -> tuple[dict, list[str]]:
    """The skeleton overlaid with what the agent wrote: unknown step ids are dropped from an expected output (the
    output goes when none is left), missing fields keep the skeleton's. Returns (card, warnings); with nothing
    parseable the skeleton comes back with authored_by: skeleton and one warning."""
    skel = skeleton or skeleton_card(flow)
    if not parsed:
        return dict(skel, authored_by="skeleton"), ["no card found in the agent's message; keeping the skeleton"]
    ids_ = {s["id"] for s in flow.get("steps") or []}
    card = dict(skel)
    warnings = []
    for k in ("use_case", "not_covered"):
        if parsed.get(k):
            card[k] = parsed[k] if k != "not_covered" else [str(x) for x in (parsed[k] if isinstance(parsed[k], list) else [parsed[k]])]
    if isinstance(parsed.get("inputs_explained"), dict):
        known = {k: str(v) for k, v in parsed["inputs_explained"].items() if k in (flow.get("inputs") or {})}
        card["inputs_explained"] = {**skel.get("inputs_explained", {}), **known}
        for k in parsed["inputs_explained"]:
            if k not in known:
                warnings.append(f"inputs_explained: unknown input {k!r} dropped")
    if isinstance(parsed.get("example"), dict):
        ex = parsed["example"]
        card["example"] = {"inputs": dict(ex.get("inputs") or skel.get("example", {}).get("inputs") or {}), "found": str(ex.get("found") or "")}
    eos = []
    for eo in parsed.get("expected_outputs") or [] if isinstance(parsed.get("expected_outputs"), list) else []:
        if not isinstance(eo, dict) or not eo.get("name"):
            warnings.append("an expected output without a name was dropped")
            continue
        steps = [s for s in (eo.get("steps") or []) if isinstance(eo.get("steps"), list)]
        keep = [s for s in steps if s in ids_]
        for s in steps:
            if s not in ids_:
                warnings.append(f"expected output {eo['name']!r}: unknown step {s!r} dropped")
        if not keep:
            warnings.append(f"expected output {eo['name']!r} dropped: none of its steps exist")
            continue
        eos.append({"name": str(eo["name"]), "steps": keep, "description": str(eo.get("description") or ""), "required": bool(eo.get("required", False))})
    if eos:
        card["expected_outputs"] = eos
    else:
        warnings.append("no usable expected_outputs in the agent's card; keeping the skeleton's")
    card["authored_by"] = authored_by
    return card, warnings


def card_prompt(flow: dict, skeleton: dict | None = None) -> str:
    """Instructions appended to the agent's prompt: finish with the card as one fenced yaml block."""
    skel = skeleton or skeleton_card(flow)
    ids_ = ", ".join(s["id"] for s in flow.get("steps") or [])
    return "\n".join([
        "Then END your message with a flow card: one fenced ```yaml block containing exactly this mapping (no other keys):",
        "```yaml",
        yaml.safe_dump({k: skel[k] for k in CARD_KEYS if k in skel}, sort_keys=False, width=120, allow_unicode=True).rstrip(),
        "```",
        "Rewrite it in your own words for a person deciding whether to run this flow: use_case (when to reach for it, 1-2 sentences), "
        "inputs_explained (one line per input), expected_outputs (one entry per kind of evidence, keep the `steps` lists as step ids, "
        "set required: true only for evidence the flow is pointless without, describe what a person gets), not_covered (what this "
        "investigation does not look at), example (the inputs you ran and one sentence of what the run found), authored_by: agent.",
        f"Step ids you may reference: {ids_}. A call you added by hand becomes a step named after its tool (for example "
        "jira_get_issue_comments, git_show); referencing one is fine, unknown ids are dropped.",
    ])


def format_card(card: dict, flow: dict | None = None) -> str:
    lines = []
    if flow:
        lines.append(f"{flow.get('name')}  [{flow.get('status', '?')}]  card by: {card.get('authored_by', '?')}")
    lines += ["use case: " + str(card.get("use_case") or "").strip(), "inputs:"]
    for k, v in (card.get("inputs_explained") or {}).items():
        lines.append(f"  {k}: {v}")
    lines.append("expected outputs:")
    for eo in card.get("expected_outputs") or []:
        req = "required" if eo.get("required") else "optional"
        lines.append(f"  - {eo.get('name')} [{req}] steps: {', '.join(eo.get('steps') or [])}")
        if eo.get("description"):
            lines.append(f"      {eo['description']}")
    if card.get("not_covered"):
        lines.append("not covered:")
        lines += [f"  - {x}" for x in card["not_covered"]]
    ex = card.get("example") or {}
    if ex.get("inputs") or ex.get("found"):
        lines.append(f"example: inputs={ex.get('inputs')}" + (f"  found: {ex['found']}" if ex.get("found") else ""))
    return "\n".join(lines)


def card_yaml(card: dict) -> str:
    """The `card:` block as YAML text, ready to append to a flow file (a top-level key added at the end of the file
    keeps the hand-written comments above it intact)."""
    return yaml.safe_dump({"card": {k: card[k] for k in CARD_KEYS if k in card}}, sort_keys=False, width=120, allow_unicode=True)
