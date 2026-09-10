"""Deterministic synthetic 'corporate world' generator.

Produces sim/data/world.json (services, people, channels, incidents with cross-linked artefacts)
and a small real git repo at sim/repo whose source contains the error strings and logger calls.
Everything is seeded so the world is identical on every run.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import re

import scenarios

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPO = ROOT / "repo"
BASE = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)

SERVICES = [
    # name, team, language-ish module, jira project, pd service id
    ("payments-api", "payments", "PAY", "PXYZ01"),
    ("checkout-web", "storefront", "STF", "PABC02"),
    ("inventory-sync", "supply", "SUP", "PDEF03"),
    ("notifier", "platform", "PLAT", "PGHI04"),
]
TEAMS = {
    "payments": ["alice", "bob"],
    "storefront": ["carol", "dan"],
    "supply": ["erin", "frank"],
    "platform": ["grace", "heidi"],
}
ALIASES = {
    # how people refer to the service in prose (DMs); becomes catalog aliases for the gazetteer
    "payments-api": ["payments", "payment gateway"],
    "checkout-web": ["checkout", "storefront checkout"],
    "inventory-sync": ["inventory sync", "inventory", "warehouse sync"],
    "notifier": ["notifications", "notification service", "sms notifier"],
}
SYMPTOMS = {
    "payments-api": "a customer says card charges are hanging",
    "checkout-web": "people can't complete checkout",
    "inventory-sync": "warehouse team says stock levels look stale",
    "notifier": "sms confirmations aren't arriving",
}
DM_NOISE = ["lunch?", "can you review my PR when you get a sec?", "thanks for the help yesterday",
            "are you around for the retro?", "quick q about the deploy pipeline, no rush", "did the staging deploy go out?"]
ERRORS = {
    "payments-api": [
        ("PaymentGatewayTimeout", "gateway did not respond within 30s", "charge"),
        ("LedgerMismatch", "ledger balance mismatch for account", "reconcile"),
    ],
    "checkout-web": [
        ("CartSerializationError", "failed to serialize cart", "render_cart"),
        ("SessionExpired", "session token expired mid-checkout", "submit_order"),
    ],
    "inventory-sync": [
        ("UpstreamSchemaDrift", "unexpected field in warehouse feed", "ingest_feed"),
        ("StaleSnapshot", "snapshot older than threshold", "publish_snapshot"),
    ],
    "notifier": [
        ("TemplateNotFound", "no template registered for event", "render"),
        ("RateLimited", "downstream SMS provider rate limited", "send_sms"),
    ],
}


def h(s: str, n: int = 12) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def ts(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def slack_ts(dt: datetime, i: int = 0) -> str:
    return f"{int(dt.timestamp())}.{i:06d}"


def build_world(seed: int = 7) -> dict:
    rnd = random.Random(seed)
    people = {}
    for team, names in TEAMS.items():
        for n in names:
            people[n] = {"id": "U" + h(n, 8).upper(), "name": n, "real_name": n.title() + " " + team.title(), "team": team}
    services = {}
    for name, team, proj, pd in SERVICES:
        services[name] = {
            "name": name,
            "team": team,
            "owners": TEAMS[team],
            "jira_project": proj,
            "slack_channel": f"#{team}-alerts",
            "incident_channel": "#incidents",
            "pagerduty_service_id": pd,
            "repo_path": f"services/{name.replace('-', '_')}",
            "aliases": ALIASES[name],
            "prom_labels": {"service": name, "env": "prod"},
            "logz_type": name,
        }
    channels = {}
    for cname in ["#incidents", "#general"] + [f"#{t}-alerts" for t in TEAMS] + [f"#team-{t}" for t in TEAMS]:
        channels[cname] = {"id": "C" + h(cname, 8).upper(), "name": cname.lstrip("#")}

    incidents = []
    jira_counter = {proj: 100 for _, _, proj, _ in SERVICES}
    day = 0
    for k in range(10):
        svc_name = SERVICES[k % len(SERVICES)][0]
        svc = services[svc_name]
        err_class, err_msg, func = ERRORS[svc_name][k % 2]
        day += rnd.randint(2, 4)
        t0 = BASE + timedelta(days=day, hours=rnd.randint(0, 9), minutes=rnd.randint(0, 59))
        proj = svc["jira_project"]
        jira_counter[proj] += rnd.randint(1, 9)
        key = f"{proj}-{jira_counter[proj]}"
        trace_ids = [h(f"trace{k}{i}", 32) for i in range(3)]
        pods = [f"{svc_name}-{h(f'rs{k}', 9)}-{h(f'pod{k}{i}', 5)}" for i in range(2)]
        error_sig = f"{err_class}: {err_msg}"
        reporter = rnd.choice(list(people))
        owner = rnd.choice(svc["owners"])
        sha = h(f"commit{k}", 40)
        inc = {
            "id": f"INC-{k+1:03d}",
            "service": svc_name,
            "team": svc["team"],
            "error_class": err_class,
            "error_sig": error_sig,
            "function": func,
            "started_at": ts(t0),
            "trace_ids": trace_ids,
            "pods": pods,
            "commit": sha,
            "jira": {
                "key": key,
                "project": proj,
                "summary": f"{svc_name}: elevated 5xx after {err_class}",
                "issuetype": "Bug",
                "priority": "P2" if k % 3 else "P1",
                "status": "Done" if k < 7 else "In Progress",
                "created": ts(t0 + timedelta(minutes=12)),
                "updated": ts(t0 + timedelta(hours=6)),
                "reporter": reporter,
                "assignee": owner,
                "components": [svc_name],
                "labels": ["incident", svc["team"]],
                "description": (
                    f"Alert fired on {svc_name} at {ts(t0)}.\n\n"
                    f"Error: {error_sig}\n"
                    f"Sample trace: trace_id={trace_ids[0]}\n"
                    f"Affected pod: {pods[0]}\n\n"
                    f"Customers saw failures in {func}(). Slack discussion in {svc['incident_channel']}. "
                    f"Runbook: {svc_name} runbook."
                ),
            },
            "pagerduty": {
                "id": "Q" + h(f"pd{k}", 13).upper(),
                "incident_number": 4000 + k,
                "title": f"[{svc_name}] {err_class} rate above threshold",
                "service_id": svc["pagerduty_service_id"],
                "status": "resolved" if k < 8 else "acknowledged",
                "urgency": "high",
                "created_at": ts(t0),
                "resolved_at": ts(t0 + timedelta(hours=2)) if k < 8 else None,
                "assignee": owner,
            },
            "slack": {
                "channel": svc["incident_channel"],
                "thread_ts": slack_ts(t0 + timedelta(minutes=4), k),
                "messages": [],
            },
            "confluence": {
                "page_id": str(20000 + k * 7),
                "title": f"{svc_name} runbook",
                "space": svc["team"].upper(),
            },
            "logs": [],
            "metric_spike": {"start": ts(t0 - timedelta(minutes=5)), "end": ts(t0 + timedelta(minutes=50))},
        }
        # Slack thread
        m0 = t0 + timedelta(minutes=4)
        msgs = [
            (owner, m0, f"Pager went off for {svc_name}: {err_class}. Looking now. Ticket {key} incoming."),
            (owner, m0 + timedelta(minutes=6), f"Seeing `{error_sig}` in logs, e.g. trace_id={trace_ids[0]} on {pods[0]}"),
            (reporter, m0 + timedelta(minutes=9), f"Also trace_id={trace_ids[1]} from a customer report, same error."),
            (owner, m0 + timedelta(minutes=20), f"Suspect {sha[:10]} touched {func}() yesterday. Rolling back."),
            (owner, m0 + timedelta(minutes=55), f"Rollback done, error rate back to baseline. Will write up in {key}."),
        ]
        for i, (who, when, text) in enumerate(msgs):
            inc["slack"]["messages"].append({"user": people[who]["id"], "user_name": who, "ts": slack_ts(when, k * 10 + i), "text": text})
        # logs
        for i in range(24):
            when = t0 - timedelta(minutes=5) + timedelta(minutes=i * 2)
            tid = trace_ids[i % 3]
            pod = pods[i % 2]
            level = "ERROR" if i % 4 else "WARN"
            inc["logs"].append({
                "@timestamp": ts(when),
                "level": level,
                "service": svc_name,
                "kubernetes.pod_name": pod,
                "trace_id": tid,
                "logger": f"{svc['repo_path'].replace('/', '.')}.handler",
                "message": f"{error_sig} (account=acc_{h(f'a{k}{i}',6)}) trace_id={tid} pod={pod} fn={func}",
            })
        incidents.append(inc)

    # noise: unrelated tickets and chatter
    noise_jira = []
    for i in range(12):
        name, team, proj, _ = SERVICES[i % len(SERVICES)]
        jira_counter[proj] += 1
        noise_jira.append({
            "key": f"{proj}-{jira_counter[proj]}",
            "project": proj,
            "summary": rnd.choice(["Upgrade dependency", "Add metrics dashboard", "Refactor config loading", "Flaky test in CI"]) + f" ({name})",
            "issuetype": "Task",
            "priority": "P3",
            "status": rnd.choice(["To Do", "In Progress", "Done"]),
            "created": ts(BASE + timedelta(days=rnd.randint(0, 30))),
            "updated": ts(BASE + timedelta(days=rnd.randint(30, 36))),
            "reporter": rnd.choice(list(people)),
            "assignee": rnd.choice(TEAMS[team]),
            "components": [name],
            "labels": ["chore"],
            "description": f"Routine work on {name}. No incident.",
        })
    noise_slack = []
    for i in range(30):
        cname = rnd.choice(list(channels))
        who = rnd.choice(list(people))
        when = BASE + timedelta(days=rnd.randint(0, 35), hours=rnd.randint(8, 18), minutes=rnd.randint(0, 59))
        noise_slack.append({"channel": cname, "user": people[who]["id"], "user_name": who, "ts": slack_ts(when, 900 + i),
                            "text": rnd.choice(["lunch?", "deploying to staging", "anyone seen the dashboard link?",
                                                "reminder: retro at 3", "PR review please", "standup in 5"])})
    pages = []
    for name, svc in services.items():
        errs = ERRORS[name]
        inc_for = [i for i in incidents if i["service"] == name]
        pid = inc_for[0]["confluence"]["page_id"] if inc_for else str(30000 + len(pages))
        body = [f"h1. {name} runbook", "", f"Owned by team {svc['team']} ({', '.join(svc['owners'])}). Alerts go to {svc['slack_channel']}; incidents are discussed in {svc['incident_channel']}.", "",
                "h2. Known failure modes", ""]
        for cls, msg, func in errs:
            body += [f"h3. {cls}", f"Symptom: log lines containing '{cls}: {msg}' from {func}().",
                     f"Check: error rate panel for service={name}; search logs for the trace_id in the alert.",
                     f"Remediation: roll back the latest deploy of {name}; if persists, page {svc['team']} on-call.", ""]
        body += ["h2. Dashboards", f"Chronosphere: rate(http_requests_total{{service=\"{name}\",code=~\"5..\"}}[5m])", ""]
        pages.append({"id": pid, "title": f"{name} runbook", "space": svc["team"].upper(), "labels": ["runbook", name],
                      "created": ts(BASE - timedelta(days=60)), "last_modified": ts(BASE + timedelta(days=3)),
                      "body": "\n".join(body)})
    for t in TEAMS:
        pages.append({"id": str(40000 + len(pages)), "title": f"Team {t} on-call guide", "space": t.upper(), "labels": ["oncall"],
                      "created": ts(BASE - timedelta(days=90)), "last_modified": ts(BASE - timedelta(days=10)),
                      "body": f"h1. Team {t} on-call\n\nRotation in PagerDuty. Escalate in #team-{t}. Services: " + ", ".join(s for s, v in services.items() if v["team"] == t)})

    # direct messages: one im channel per person with "me" (the on-call engineer). Each incident produces one DM
    # from a colleague outside the owning team, phrased one of three ways: ticket key / error class + service alias /
    # service alias + symptom. A follow-up carries a trace id a customer pasted. Plus noise DMs.
    me = {"id": "U" + h("me", 8).upper(), "name": "me", "real_name": "On-call Engineer", "team": "oncall"}
    dms = {n: {"id": "D" + h(f"dm:{n}", 8).upper(), "user": p["id"], "user_name": n, "messages": []} for n, p in people.items()}
    for k, inc in enumerate(incidents):
        svc_name, svc = inc["service"], services[inc["service"]]
        alias = ALIASES[svc_name][k % len(ALIASES[svc_name])]
        sender = rnd.choice([n for n, p in people.items() if p["team"] != svc["team"]])
        t0 = datetime.fromisoformat(inc["started_at"].replace("Z", "+00:00"))
        when = t0 + timedelta(minutes=rnd.randint(25, 95))
        key, cls = inc["jira"]["key"], inc["error_class"]
        hour = (t0 - timedelta(minutes=t0.minute)).strftime("%-I%p").lower()
        mode = k % 3
        if mode == 0:
            text = rnd.choice([f"hey, are you on {key}? support is getting pinged about it and I have nothing to tell them",
                               f"quick one: any progress on {key}? {alias} customers are asking for an update"])
        elif mode == 1:
            text = rnd.choice([f"hey, are you seeing {alias} failures? customers report {cls} since about {hour}",
                               f"heads up, support has a few reports of {cls} errors in {alias} since {hour}, is that known?"])
        else:
            text = rnd.choice([f"is {alias} healthy? {SYMPTOMS[svc_name]}",
                               f"something off with {alias} since about {hour}: {SYMPTOMS[svc_name]}. anyone looking?"])
        follow = (f"fwiw one of them pasted this from the error page: trace_id={inc['trace_ids'][2]}" if mode else
                  f"they also sent a screenshot mentioning pod {inc['pods'][1]}")
        thread = [(sender, when, text), ("me", when + timedelta(minutes=3), "on it, will update here"),
                  (sender, when + timedelta(minutes=8), follow)]
        for i, (who, at, txt) in enumerate(thread):
            u = me if who == "me" else people[who]
            dms[sender]["messages"].append({"user": u["id"], "user_name": who, "ts": slack_ts(at, 500 + k * 10 + i), "text": txt})
        inc["dm"] = {"channel_id": dms[sender]["id"], "ts": dms[sender]["messages"][-3]["ts"], "user_name": sender, "text": text, "mode": mode}
    for n, d in dms.items():
        for i in range(rnd.randint(2, 4)):
            at = BASE + timedelta(days=rnd.randint(0, 35), hours=rnd.randint(8, 18), minutes=rnd.randint(0, 59))
            who = n if i % 2 == 0 else "me"
            u = me if who == "me" else people[n]
            d["messages"].append({"user": u["id"], "user_name": who, "ts": slack_ts(at, 700 + i), "text": rnd.choice(DM_NOISE) if who == n else "sure, later today"})
        d["messages"].sort(key=lambda m: float(m["ts"]))

    data = {
        "generated_from_seed": seed,
        "me": me,
        "dms": dms,
        "base_time": ts(BASE),
        "services": services,
        "teams": TEAMS,
        "people": people,
        "channels": channels,
        "incidents": incidents,
        "noise": {"jira": noise_jira, "slack": noise_slack},
        "confluence_pages": pages,
    }
    # Additive scenario data (cascades, repeats, escalations, deploys, still-open incidents, on-call
    # rotations, cross-referencing comments, an architecture/postmortem web, noise): appended with its
    # own seeded RNG stream, after everything above. See sim/scenarios.py and docs/sim-world.md.
    # ERRORS is mutated so build_repo()'s handler-stub loop below picks up the new services' error
    # classes automatically.
    scenarios.extend(data, ERRORS, seed=seed)
    return data


# ---------------------------------------------------------------- git repo
HANDLER_TEMPLATE = '''"""{service} request handlers."""
import logging

log = logging.getLogger("{logger}")


class {cls}(Exception):
    pass


def {func}(request):
    """Handle {func} for {service}."""
    try:
        return _do_{func}(request)
    except Exception as exc:  # noqa: BLE001
        log.error("{cls}: {msg} (account=%s) trace_id=%s pod=%s fn={func}", request.account, request.trace_id, request.pod)
        raise {cls}(str(exc)) from exc


def _do_{func}(request):
    raise NotImplementedError
'''


def git(args, cwd, env=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


ORIGINAL_SERVICE_NAMES = [n for n, *_ in SERVICES]


def _write_service_files(world: dict, name: str) -> None:
    svc = world["services"][name]
    d = REPO / svc["repo_path"]
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    parts = []
    for cls, msg, func in ERRORS[name]:
        parts.append(HANDLER_TEMPLATE.format(service=name, logger=svc["repo_path"].replace("/", ".") + ".handler", cls=cls, msg=msg, func=func))
    (d / "handler.py").write_text("\n\n".join(parts))
    (d / "config.py").write_text(f"SERVICE = {name!r}\nTEAM = {svc['team']!r}\nTIMEOUT_S = 30\n")


def _write_codeowners(world: dict, names) -> None:
    (REPO / "CODEOWNERS").write_text("".join(f"services/{n.replace('-', '_')}/ @{world['services'][n]['team']}\n" for n in names))


def _commit_incident(world: dict, inc: dict) -> None:
    """One commit touching the handler of the incident's function, dated the day before. Sets inc['commit']
    to the real sha and fixes up any Slack message that named a placeholder sha (the 'Suspect <sha>...'
    guess, or a scenario overlay's now-real-but-wrong sha from another incident)."""
    svc = world["services"][inc["service"]]
    f = REPO / svc["repo_path"] / "handler.py"
    src = f.read_text()
    marker = f"def _do_{inc['function']}(request):\n    raise NotImplementedError\n"
    new = f"def _do_{inc['function']}(request):\n    # {inc['id']}: tightened validation\n    raise NotImplementedError\n"
    src = src.replace(marker, new, 1) if marker in src else src + f"\n# touched for {inc['id']}\n"
    f.write_text(src)
    when = datetime.fromisoformat(inc["started_at"].replace("Z", "+00:00")) - timedelta(days=1)
    env = {**os.environ, "GIT_AUTHOR_DATE": ts(when), "GIT_COMMITTER_DATE": ts(when),
           "GIT_AUTHOR_NAME": inc["jira"]["assignee"], "GIT_AUTHOR_EMAIL": f"{inc['jira']['assignee']}@example.com"}
    git(["add", "-A"], REPO)
    git(["commit", "-q", "-m", f"{inc['service']}: tighten validation in {inc['function']}"], REPO, env)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    inc["commit"] = sha
    for m in inc["slack"]["messages"]:
        if "Suspect " in m["text"]:
            m["text"] = f"Suspect {sha[:10]} touched {inc['function']}() yesterday. Rolling back."
    # deploy-preceded incidents (sim/scenarios.py): the deploy announcement names this same commit
    dep_id = inc.get("deploy_id")
    if dep_id:
        for d in world.get("deploys", []):
            if d["id"] == dep_id:
                d["sha"] = sha


def build_repo(world: dict) -> None:
    if REPO.exists():
        subprocess.run(["rm", "-rf", str(REPO)], check=True)
    REPO.mkdir(parents=True)
    git(["init", "-q", "-b", "main"], REPO)
    git(["config", "user.email", "bot@example.com"], REPO)
    git(["config", "user.name", "sim bot"], REPO)
    (REPO / "README.md").write_text("# sim monorepo\n\nSynthetic services for mcp_explorer.\n")
    # Tree of the initial commit must stay byte-identical (it fixes the first 11 SHAs), so it is built
    # from exactly the original 4 services, in their original order -- never from world["services"],
    # which by now also holds sim/scenarios.py's new services.
    _write_codeowners(world, ORIGINAL_SERVICE_NAMES)
    for name in ORIGINAL_SERVICE_NAMES:
        _write_service_files(world, name)
    env = {**os.environ, "GIT_AUTHOR_DATE": world["base_time"], "GIT_COMMITTER_DATE": world["base_time"]}
    git(["add", "-A"], REPO)
    git(["commit", "-q", "-m", "initial services"], REPO, env)

    original_incidents = [i for i in world["incidents"] if i["service"] in ORIGINAL_SERVICE_NAMES]
    scenario_incidents = [i for i in world["incidents"] if i["service"] not in ORIGINAL_SERVICE_NAMES]
    for inc in original_incidents:
        _commit_incident(world, inc)

    new_service_names = [n for n in world["services"] if n not in ORIGINAL_SERVICE_NAMES]
    if new_service_names:
        # sim/scenarios.py's services: their own commit, after the first 11, so earlier SHAs are untouched.
        # Dated the day before the first scenario incident's own commit -- strictly after every original
        # incident commit's date -- so `git log --since/--until` (which assumes date order) never has to
        # skip over an out-of-order commit while walking a service's path history.
        earliest = min(i["started_at"] for i in scenario_incidents)
        new_commit_when = datetime.fromisoformat(earliest.replace("Z", "+00:00")) - timedelta(days=2)
        new_env = {**os.environ, "GIT_AUTHOR_DATE": ts(new_commit_when), "GIT_COMMITTER_DATE": ts(new_commit_when)}
        for name in new_service_names:
            _write_service_files(world, name)
        _write_codeowners(world, ORIGINAL_SERVICE_NAMES + new_service_names)
        git(["add", "-A"], REPO)
        git(["commit", "-q", "-m", "add new services"], REPO, new_env)
    # Committed in chronological order (not necessarily INC-id order -- a repeat pair's second incident can
    # land after the next pair's first) so `git log --since/--until`'s date-order assumption never breaks.
    for inc in sorted(scenario_incidents, key=lambda i: i["started_at"]):
        _commit_incident(world, inc)

    # sim/scenarios.py defers every commit-sha reference it cannot know yet (its own incident's commit is
    # still a placeholder when it runs, let alone another incident's) as a {{SHA:ID}}/{{SHA10:ID}}/
    # {{WRONG_SHA:ID}} token, in Slack text, Jira comments, and confluence page bodies/links alike. Now
    # that every incident's commit is final, resolve them everywhere in one generic pass.
    sha_by_id = {i["id"]: i["commit"] for i in world["incidents"]}

    def _resolve_sha_placeholders(obj):
        if isinstance(obj, str):
            obj = re.sub(r"\{\{SHA10:(INC-\d+)\}\}", lambda mo: sha_by_id[mo.group(1)][:10], obj)
            obj = re.sub(r"\{\{WRONG_SHA:(INC-\d+)\}\}", lambda mo: sha_by_id[mo.group(1)][:10], obj)
            obj = re.sub(r"\{\{SHA:(INC-\d+)\}\}", lambda mo: sha_by_id[mo.group(1)], obj)
            return obj
        if isinstance(obj, list):
            return [_resolve_sha_placeholders(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _resolve_sha_placeholders(v) for k, v in obj.items()}
        return obj

    world["incidents"] = _resolve_sha_placeholders(world["incidents"])
    world["confluence_pages"] = _resolve_sha_placeholders(world["confluence_pages"])
    if world.get("deploys"):
        world["deploys"] = _resolve_sha_placeholders(world["deploys"])


def main() -> None:
    world = build_world()
    build_repo(world)
    DATA.mkdir(exist_ok=True)
    (DATA / "world.json").write_text(json.dumps(world, indent=1))
    print(f"world: {len(world['incidents'])} incidents, {len(world['services'])} services, "
          f"{len(world['confluence_pages'])} pages -> {DATA / 'world.json'}; repo at {REPO}")


if __name__ == "__main__":
    main()
