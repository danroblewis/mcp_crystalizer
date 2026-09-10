"""Scenario data appended to the synthetic world.

Adds cascading multi-service incidents, repeat-incident pairs with postmortems, incidents escalated
from a team channel, deploy-preceded incidents, still-open incidents, on-call rotations, cross-referencing
Jira comments, a runbook -> architecture -> postmortem documentation web, and incident-adjacent Slack
noise. Everything here is generated with its own seeded RNG stream and appended after the existing
(byte-identical) generation in sim/world.py -- see docs/sim-world.md for a guided tour.

Called once from sim/world.py's build_world(), after the original 10 incidents / 4 services / 8 people /
10 channels / 8 pages are built, and before build_repo() runs (so the new incidents' commits are appended
to git history after the first 10, in creation order).
"""
from __future__ import annotations

import hashlib
import itertools
import random
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)


def _h(s: str, n: int = 12) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def _ts(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _slack_ts(dt: datetime, i: int) -> str:
    return f"{int(dt.timestamp())}.{i:06d}"


def _sha(inc_id: str) -> str:
    """Full commit sha placeholder, resolved by sim/world.py's build_repo() once every incident's real
    commit is known (this module runs before commits exist)."""
    return f"{{{{SHA:{inc_id}}}}}"


def _sha10(inc_id: str) -> str:
    """Abbreviated commit sha placeholder, same deferred resolution as _sha()."""
    return f"{{{{SHA10:{inc_id}}}}}"


# ---------------------------------------------------------------- static scenario data
NEW_TEAMS = ["identity", "eventbus", "search", "billing"]

TEAM_ROSTER_NAMES = {
    "identity": ["ivan", "judy", "mallory", "niaj", "olivia", "peggy", "quentin", "rupert", "sybil", "trent"],
    "eventbus": ["ursula", "victor", "wendy", "xavier", "yolanda", "zach", "aaron", "brenda", "chloe", "derek"],
    "search": ["elena", "felix", "gina", "hank", "iris", "jack", "kara", "liam", "maya", "noah"],
    "billing": ["opal", "priya", "quinn", "ravi", "sara", "tariq", "uma", "vince", "wanda", "xena"],
}

# name, team, jira project code, [(error_class, message, function), ...], [aliases], symptom
NEW_SERVICES = [
    ("auth-service", "identity", "AUTH", [
        ("AuthTokenExpired", "auth token expired mid-request", "validate_token"),
        ("IdentityProviderTimeout", "identity provider did not respond within 10s", "authenticate"),
    ], ["auth", "authentication service"], "users are getting logged out mid-session"),
    ("sso-gateway", "identity", "SSO", [
        ("SSOHandshakeError", "handshake with identity provider failed", "login"),
        ("CertificateExpired", "tls certificate expired", "handshake"),
    ], ["sso", "single sign-on"], "SSO login is failing for enterprise customers"),
    ("directory-service", "identity", "DIR", [
        ("DirectorySyncDrift", "directory entry drifted from source of truth", "sync_directory"),
        ("GroupMembershipMismatch", "group membership cache mismatch", "resolve_groups"),
    ], ["directory", "user directory"], "some users have the wrong permissions"),
    ("event-bus", "eventbus", "EVT", [
        ("EventPublishTimeout", "publish to event bus timed out", "publish_event"),
        ("ConsumerLagError", "consumer lag exceeded threshold", "consume_stream"),
    ], ["event bus", "eventing"], "downstream services are missing events"),
    ("webhook-dispatcher", "eventbus", "WHK", [
        ("WebhookDeliveryError", "webhook delivery failed after retries", "dispatch_webhook"),
        ("RetryQueueLimited", "retry queue exceeded capacity", "enqueue_retry"),
    ], ["webhooks", "webhook service"], "partner webhooks aren't arriving"),
    ("stream-processor", "eventbus", "STRM", [
        ("StreamProcessingTimeout", "stream processing fell behind", "process_stream"),
        ("CheckpointMismatch", "checkpoint state was corrupted", "checkpoint"),
    ], ["stream processor", "stream processing"], "real-time dashboards look stale"),
    ("search-api", "search", "SRCH", [
        ("SearchIndexNotFound", "search index is missing for tenant", "search"),
        ("QueryTimeout", "query exceeded timeout", "search"),
    ], ["search", "search service"], "product search is returning no results"),
    ("indexer", "search", "IDX", [
        ("IndexBuildError", "index build failed", "build_index"),
        ("ShardNotFound", "shard is unavailable", "route_shard"),
    ], ["indexer", "search indexer"], "search results are out of date"),
    ("autocomplete-service", "search", "ACS", [
        ("SuggestionCacheMismatch", "suggestion cache mismatch storm", "serve_suggestions"),
        ("AutocompleteLatencyTimeout", "autocomplete latency exceeded budget", "rank_suggestions"),
    ], ["autocomplete", "typeahead"], "search-as-you-type is lagging badly"),
    ("billing-service", "billing", "BILL", [
        ("InvoiceCalculationError", "invoice calculation produced a negative total", "calculate_invoice"),
        ("PaymentPlanMismatch", "payment plan mismatch detected", "apply_plan"),
    ], ["billing", "billing service"], "customers are being billed the wrong amount"),
    ("invoicing", "billing", "INVC", [
        ("InvoiceRenderError", "invoice render failed", "render_invoice"),
        ("TaxCalculationError", "tax calculation error", "calculate_tax"),
    ], ["invoicing", "invoice service"], "customers can't download their invoices"),
]

MISC_CHANNELS = [
    "#random", "#announcements", "#watercooler", "#eng-all", "#support", "#product", "#hiring",
    "#oncall-handoff", "#retro", "#postmortems", "#travel", "#books-club", "#music", "#pets",
    "#gardening", "#coffee", "#photography", "#gaming", "#running-club", "#book-recs",
    "#career-dev", "#interview-prep", "#security-notices", "#compliance", "#legal",
    "#finance-ops", "#facilities", "#it-helpdesk", "#new-hires", "#swag",
]

FILLER_PAGE_TITLES = [
    "Onboarding guide", "Incident response process", "SLA policy", "Security guidelines",
    "API style guide", "Testing strategy", "On-call escalation policy", "Data retention policy",
    "Glossary of service names", "Postmortem template", "Architecture decision records index",
    "Release process", "Feature flag guide", "Vendor management", "Compliance checklist",
]

NOISE_PHRASES = [
    "anyone else seeing timeouts on the api gateway today?",
    "release notes for this week are up, mostly rate limited retries cleanup",
    "reminder: postmortem retro at 3pm, bring your incident notes",
    "is it just me or is checkout kind of slow right now?",
    "heads up, deploying a config change to the event bus, should be a no-op",
    "search feels rate limited again, anyone looking?",
    "fyi trace_id abcd1234abcd1234abcd1234abcd1234 was in a support ticket, not sure it's real",
    "billing dashboard timeout when I load the last 90 days, known issue?",
    "auth flakiness in staging only as far as I can tell",
    "does anyone have the runbook link for the identity service handy?",
]


def _oncall_person(rotation: list[dict], when: datetime) -> str:
    for w in rotation:
        start = datetime.fromisoformat(w["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(w["end"].replace("Z", "+00:00"))
        if start <= when < end:
            return w["person"]
    return rotation[-1]["person"]


def extend(world: dict, errors_out: dict, seed: int = 7) -> None:
    """Mutate `world` in place, appending scenario entities. `errors_out` is sim/world.py's module-level
    ERRORS dict: build_repo() reads it by service name to write the initial handler.py stubs and per-incident
    commits, so new service names must be registered there for the repo build to pick them up automatically.
    """
    rnd = random.Random(seed * 1000 + 1)
    ts_counter = itertools.count(20000)

    people = world["people"]
    teams = world["teams"]
    services = world["services"]
    channels = world["channels"]
    pages = world["confluence_pages"]
    incidents = world["incidents"]
    noise_slack = world["noise"]["slack"]

    # ---------------------------------------------------------------- people & teams
    for team in NEW_TEAMS:
        names = TEAM_ROSTER_NAMES[team]
        teams[team] = list(names)
        for n in names:
            people[n] = {"id": "U" + _h(n, 8).upper(), "name": n, "real_name": n.title() + " " + team.title(), "team": team}

    # ---------------------------------------------------------------- services
    for name, team, proj, errors, aliases, symptom in NEW_SERVICES:
        errors_out[name] = errors
        services[name] = {
            "name": name,
            "team": team,
            "owners": TEAM_ROSTER_NAMES[team][:3],
            "jira_project": proj,
            "slack_channel": f"#{team}-alerts",
            "incident_channel": "#incidents",
            "pagerduty_service_id": "P" + _h(f"pd:{name}", 6).upper(),
            "repo_path": f"services/{name.replace('-', '_')}",
            "aliases": aliases,
            "prom_labels": {"service": name, "env": "prod"},
            "logz_type": name,
        }
    new_service_names = [n for n, *_ in NEW_SERVICES]
    services_by_team = {t: [n for n, tm, *_ in NEW_SERVICES if tm == t] for t in NEW_TEAMS}
    errors_by_service = {n: e for n, _, _, e, _, _ in NEW_SERVICES}
    symptom_by_service = {n: s for n, _, _, _, _, s in NEW_SERVICES}

    # ---------------------------------------------------------------- channels
    for team in NEW_TEAMS:
        for cname in (f"#{team}-alerts", f"#team-{team}"):
            channels[cname] = {"id": "C" + _h(cname, 8).upper(), "name": cname.lstrip("#")}
    channels["#deploys"] = {"id": "C" + _h("#deploys", 8).upper(), "name": "deploys"}
    for cname in MISC_CHANNELS:
        channels[cname] = {"id": "C" + _h(cname, 8).upper(), "name": cname.lstrip("#")}

    # ---------------------------------------------------------------- on-call rotations
    # New teams: 45-day windows across the whole simulated period (through day ~365), 3 people cycling.
    # Old teams: one informational window over their existing two owners (existing incidents predate
    # rotation and are not required to match it).
    period_end = BASE + timedelta(days=365)
    window_len = timedelta(days=45)
    rotations: dict[str, list[dict]] = {}
    for team in NEW_TEAMS:
        regulars = TEAM_ROSTER_NAMES[team][:3]
        windows = []
        t = BASE
        i = 0
        while t < period_end:
            end = min(t + window_len, period_end)
            windows.append({"person": regulars[i % len(regulars)], "start": _ts(t), "end": _ts(end)})
            t = end
            i += 1
        rotations[team] = windows
    for name, owners in [("payments", ["alice", "bob"]), ("storefront", ["carol", "dan"]),
                          ("supply", ["erin", "frank"]), ("platform", ["grace", "heidi"])]:
        rotations[name] = [{"person": owners[0], "start": _ts(BASE), "end": _ts(period_end)}]
    world["oncall_rotations"] = rotations

    # ---------------------------------------------------------------- id counters
    next_id_n = [len(incidents) + 1]

    def new_inc_id() -> str:
        s = f"INC-{next_id_n[0]:03d}"
        next_id_n[0] += 1
        return s

    jira_counter = {proj: 100 for _, _, proj, *_ in NEW_SERVICES}

    def new_key(proj: str) -> str:
        jira_counter[proj] += rnd.randint(1, 9)
        return f"{proj}-{jira_counter[proj]}"

    def pd_id(tag: str) -> str:
        return "Q" + _h(f"pd:{tag}", 13).upper()

    def dm_slack_ts(dt: datetime) -> str:
        return _slack_ts(dt, next(ts_counter))

    def permalink(ch_id: str, ts: str) -> str:
        return f"https://sim.slack.com/archives/{ch_id}/p{ts.replace('.', '')}"

    def owner_for(team: str, when: datetime) -> str:
        return _oncall_person(rotations[team], when)

    def make_incident(svc_name: str, err_class: str, err_msg: str, func: str, t0: datetime,
                       trace_ids: list[str] | None = None, resolved: bool = True) -> dict:
        svc = services[svc_name]
        inc_id = new_inc_id()
        idx = int(inc_id.split("-")[1])
        trace_ids = trace_ids or [_h(f"tr{idx}{i}", 32) for i in range(3)]
        pods = [f"{svc_name}-{_h(f'rs{idx}', 9)}-{_h(f'pod{idx}{i}', 5)}" for i in range(2)]
        error_sig = f"{err_class}: {err_msg}"
        key = new_key(svc["jira_project"])
        team = svc["team"]
        owner = owner_for(team, t0)
        reporter = rnd.choice([n for n in TEAM_ROSTER_NAMES.get(team, [owner]) if n != owner] or [owner])
        inc = {
            "id": inc_id,
            "service": svc_name,
            "team": team,
            "error_class": err_class,
            "error_sig": error_sig,
            "function": func,
            "started_at": _ts(t0),
            "trace_ids": trace_ids,
            "pods": pods,
            "commit": _h(f"commit{inc_id}", 40),
            "jira": {
                "key": key, "project": svc["jira_project"],
                "summary": f"{svc_name}: elevated 5xx after {err_class}",
                "issuetype": "Bug", "priority": "P2" if idx % 3 else "P1",
                "status": "Done" if resolved else "In Progress",
                "created": _ts(t0 + timedelta(minutes=12)), "updated": _ts(t0 + timedelta(hours=6)),
                "reporter": reporter, "assignee": owner, "components": [svc_name],
                "labels": ["incident", team],
                "description": (
                    f"Alert fired on {svc_name} at {_ts(t0)}.\n\n"
                    f"Error: {error_sig}\n"
                    f"Sample trace: trace_id={trace_ids[0]}\n"
                    f"Affected pod: {pods[0]}\n\n"
                    f"Customers saw failures in {func}(). Slack discussion in {svc['incident_channel']}. "
                    f"Runbook: {svc_name} runbook."
                ),
            },
            "pagerduty": {
                "id": pd_id(inc_id), "incident_number": 9000 + idx,
                "title": f"[{svc_name}] {err_class} rate above threshold",
                "service_id": svc["pagerduty_service_id"],
                "status": "resolved" if resolved else "triggered",
                "urgency": "high", "created_at": _ts(t0),
                "resolved_at": _ts(t0 + timedelta(hours=2)) if resolved else None,
                "assignee": owner,
            },
            "slack": {"channel": svc["incident_channel"], "thread_ts": dm_slack_ts(t0 + timedelta(minutes=4)), "messages": []},
            "confluence": {"page_id": None, "title": f"{svc_name} runbook", "space": team.upper()},
            "logs": [],
            "metric_spike": {"start": _ts(t0 - timedelta(minutes=5)), "end": _ts(t0 + timedelta(minutes=50))},
        }
        return inc

    def add_logs(inc: dict, trace_ids: list[str], pods: list[str], count: int = 24) -> None:
        t0 = datetime.fromisoformat(inc["started_at"].replace("Z", "+00:00"))
        svc = services[inc["service"]]
        for i in range(count):
            when = t0 - timedelta(minutes=5) + timedelta(minutes=i * 2)
            tid = trace_ids[i % len(trace_ids)]
            pod = pods[i % len(pods)]
            level = "ERROR" if i % 4 else "WARN"
            acct = _h(f"a{inc['id']}{i}", 6)
            inc["logs"].append({
                "@timestamp": _ts(when), "level": level, "service": inc["service"],
                "kubernetes.pod_name": pod, "trace_id": tid,
                "logger": f"{svc['repo_path'].replace('/', '.')}.handler",
                "message": f"{inc['error_sig']} (account=acc_{acct}) trace_id={tid} pod={pod} fn={inc['function']}",
            })

    def add_msg(inc: dict, who: str, when: datetime, text: str) -> dict:
        m = {"user": people[who]["id"], "user_name": who, "ts": dm_slack_ts(when), "text": text}
        inc["slack"]["messages"].append(m)
        return m

    def standard_thread(inc: dict, t0: datetime, owner: str, reporter: str, closing: bool = True) -> None:
        m0 = t0 + timedelta(minutes=4)
        add_msg(inc, owner, m0, f"Pager went off for {inc['service']}: {inc['error_class']}. Looking now. Ticket {inc['jira']['key']} incoming.")
        add_msg(inc, owner, m0 + timedelta(minutes=6), f"Seeing `{inc['error_sig']}` in logs, e.g. trace_id={inc['trace_ids'][0]} on {inc['pods'][0]}")
        add_msg(inc, reporter, m0 + timedelta(minutes=9), f"Also trace_id={inc['trace_ids'][1]} from a customer report, same error.")
        add_msg(inc, owner, m0 + timedelta(minutes=20), f"Suspect {inc['commit'][:10]} touched {inc['function']}() yesterday. Rolling back.")
        if closing:
            add_msg(inc, owner, m0 + timedelta(minutes=55), f"Rollback done, error rate back to baseline. Will write up in {inc['jira']['key']}.")
        else:
            add_msg(inc, owner, m0 + timedelta(minutes=55), "Still digging into this one, no root cause yet. Will keep this thread updated.")

    def finalize(inc: dict, resolved: bool = True) -> None:
        svc = services[inc["service"]]
        inc["confluence"]["page_id"] = runbook_id[inc["service"]]
        add_logs(inc, inc["trace_ids"], inc["pods"])
        t0 = datetime.fromisoformat(inc["started_at"].replace("Z", "+00:00"))
        standard_thread(inc, t0, inc["jira"]["assignee"], inc["jira"]["reporter"], closing=resolved)

    # ---------------------------------------------------------------- confluence page ids (pre-allocated)
    pid_counter = itertools.count(50000)
    runbook_id = {n: str(next(pid_counter)) for n in new_service_names}
    oncall_guide_id = {t: str(next(pid_counter)) for t in NEW_TEAMS}
    arch_id = {"identity": str(next(pid_counter)), "eventbus": str(next(pid_counter)),
               "search": str(next(pid_counter)), "billing": str(next(pid_counter)),
               "cross1": str(next(pid_counter)), "cross2": str(next(pid_counter))}
    postmortem_id = [str(next(pid_counter)) for _ in range(6)]
    filler_id = [str(next(pid_counter)) for _ in range(len(FILLER_PAGE_TITLES))]

    new_incidents: list[dict] = []

    # =================================================================== 1. cascading multi-service incidents
    cascade_groups = [
        ("auth-service", "sso-gateway", "directory-service"),
        ("event-bus", "webhook-dispatcher", "stream-processor"),
        ("search-api", "indexer", "autocomplete-service"),
        ("billing-service", "invoicing", "auth-service"),
        ("directory-service", "event-bus", "search-api"),
    ]
    cascade_downstreams_for_herrings = []
    for gi, (up_svc, down1_svc, down2_svc) in enumerate(cascade_groups):
        day = 45 + gi * 12
        t0 = BASE + timedelta(days=day, hours=rnd.randint(0, 8), minutes=rnd.randint(0, 59))
        up_cls, up_msg, up_func = errors_by_service[up_svc][gi % 2]
        up = make_incident(up_svc, up_cls, up_msg, up_func, t0)
        shared_tid = up["trace_ids"][0]

        downs = []
        for j, down_svc in enumerate((down1_svc, down2_svc)):
            dt0 = t0 + timedelta(minutes=6 + j * 5)
            d_cls, d_msg, d_func = errors_by_service[down_svc][(gi + j + 1) % 2]
            herrings = [_h(f"herring{up['id']}{j}{i}", 32) for i in range(1 + (j % 2))]
            down = make_incident(down_svc, d_cls, d_msg, d_func, dt0, trace_ids=[shared_tid] + herrings)
            downs.append(down)

        up["child_incidents"] = [d["id"] for d in downs]
        up["jira"]["issuelinks"] = [{"type": "causes", "key": d["jira"]["key"]} for d in downs]
        up["pagerduty"]["related_incidents"] = [d["pagerduty"]["id"] for d in downs]
        finalize(up)

        for j, down in enumerate(downs):
            down["parent_incident"] = up["id"]
            down["jira"]["issuelinks"] = [{"type": "is caused by", "key": up["jira"]["key"]}]
            down["pagerduty"]["related_incidents"] = [up["pagerduty"]["id"]]
            svc = services[down["service"]]
            down["confluence"]["page_id"] = runbook_id[down["service"]]
            add_logs(down, down["trace_ids"], down["pods"])
            down_owner = down["jira"]["assignee"]
            dt0 = datetime.fromisoformat(down["started_at"].replace("Z", "+00:00"))
            up_permalink = permalink(channels[up["slack"]["channel"]]["id"], up["slack"]["thread_ts"])
            # downstream's own thread lives in its team's alerts channel, not #incidents
            team_chan = svc["slack_channel"]
            down["slack"]["channel"] = team_chan
            down["slack"]["thread_ts"] = dm_slack_ts(dt0 + timedelta(minutes=2))
            add_msg(down, down_owner, dt0 + timedelta(minutes=2),
                    f"Elevated {down['error_class']} on {down['service']}, trace_id={shared_tid} showing up here too.")
            add_msg(down, down_owner, dt0 + timedelta(minutes=4),
                    f"Same root cause as {up['id']} ({up['jira']['key']}), see {up_permalink}")
            add_msg(down, down_owner, dt0 + timedelta(minutes=10),
                    f"Given the upstream fix, expect this to clear on its own. Monitoring {down['service']}.")
            down_permalink = permalink(channels[team_chan]["id"], down["slack"]["thread_ts"])
            add_msg(up, down_owner, dt0 + timedelta(minutes=12),
                    f"Confirmed impact on {down['service']} from this too, see {down_permalink}")
            cascade_downstreams_for_herrings.append(down["id"])
        new_incidents.append(up)
        new_incidents.extend(downs)

    cascade_upstream_ids = [i["id"] for i in new_incidents if "child_incidents" in i]

    # =================================================================== 2. repeat incident pairs + postmortems
    repeat_specs = [
        ("auth-service", 0), ("event-bus", 1), ("webhook-dispatcher", 0),
        ("search-api", 1), ("billing-service", 0), ("invoicing", 1),
    ]
    repeat_second_ids = []
    postmortem_pages = []
    for pi, (svc_name, err_idx) in enumerate(repeat_specs):
        cls, msg, func = errors_by_service[svc_name][err_idx]
        day1 = 100 + pi * 25
        t1 = BASE + timedelta(days=day1, hours=rnd.randint(0, 8), minutes=rnd.randint(0, 59))
        first = make_incident(svc_name, cls, msg, func, t1)
        finalize(first)

        gap_days = rnd.randint(21, 56)
        t2 = t1 + timedelta(days=gap_days)
        second = make_incident(svc_name, cls, msg, func, t2)
        second["repeat_of"] = first["id"]
        second["jira"]["description"] += (
            f"\n\nThis looks like {first['jira']['key']} again -- same error class, same service."
        )
        finalize(second)
        second["slack"]["messages"][0]["text"] = (
            f"Pager went off again for {svc_name}: {cls}. This looks like {first['jira']['key']} again. "
            f"Ticket {second['jira']['key']} incoming."
        )

        pm_id = postmortem_id[pi]
        team = services[svc_name]["team"]
        arch_page = arch_id[team]
        pm = {
            "id": pm_id, "title": f"{svc_name} postmortem: {cls} recurrence",
            "space": team.upper(), "labels": ["postmortem", svc_name],
            "created": _ts(t2 + timedelta(days=2)), "last_modified": _ts(t2 + timedelta(days=2)),
            "parent_id": runbook_id[svc_name],
            "links": [first["jira"]["key"], second["jira"]["key"], _sha(first["id"]), _sha(second["id"]), arch_page],
            "body": (
                f"h1. {svc_name} postmortem: {cls} recurrence\n\n"
                f"h2. Summary\nTwo incidents on {svc_name} with the same error class {cls}: "
                f"{first['jira']['key']} and {second['jira']['key']}, {gap_days} days apart. "
                f"The first fix (commit {_sha10(first['id'])}) did not fully address the root cause; "
                f"the second incident was resolved with commit {_sha10(second['id'])}.\n\n"
                f"h2. Timeline\n* {first['jira']['key']} at {first['started_at']}\n"
                f"* {second['jira']['key']} at {second['started_at']}\n\n"
                f"h2. Follow-ups\nSee the {svc_name} runbook and the {team} architecture page for context."
            ),
        }
        postmortem_pages.append(pm)
        new_incidents.append(first)
        new_incidents.append(second)
        repeat_second_ids.append(second["id"])

    # =================================================================== 3. escalated-from-team-channel incidents
    escalation_specs = [
        ("sso-gateway", 1), ("directory-service", 1), ("stream-processor", 1),
        ("indexer", 1), ("autocomplete-service", 0), ("invoicing", 0),
    ]
    escalation_ids = []
    for ei, (svc_name, err_idx) in enumerate(escalation_specs):
        cls, msg, func = errors_by_service[svc_name][err_idx]
        day = 260 + ei * 6
        t0 = BASE + timedelta(days=day, hours=rnd.randint(0, 8), minutes=rnd.randint(0, 59))
        inc = make_incident(svc_name, cls, msg, func, t0)
        team = services[svc_name]["team"]
        team_chan = services[svc_name]["slack_channel"]
        owner = inc["jira"]["assignee"]

        pre_start = t0 - timedelta(minutes=rnd.randint(15, 30))
        pre_ts = dm_slack_ts(pre_start)
        pre_msgs = [
            (owner, pre_start, f"Seeing something odd on {svc_name}, maybe {cls}? Still triaging, not sure it's a real incident yet."),
            (owner, pre_start + timedelta(minutes=5), f"Yeah, trace_id={inc['trace_ids'][0]} confirms it. Escalating."),
        ]
        # canonical thread first (need its ts before writing the escalation permalink into the team thread)
        canonical_ts = dm_slack_ts(t0 + timedelta(minutes=4))
        inc["slack"]["thread_ts"] = canonical_ts
        canonical_permalink = permalink(channels[inc["slack"]["channel"]]["id"], canonical_ts)
        team_permalink = permalink(channels[team_chan]["id"], pre_ts)

        # write the team-channel thread (as its own noise-like slack thread, not tied to `inc`)
        team_thread_msgs = []
        for who, when, text in pre_msgs:
            team_thread_msgs.append({"user": people[who]["id"], "user_name": who, "ts": pre_ts if when == pre_start else dm_slack_ts(when), "text": text})
        team_thread_msgs.append({"user": people[owner]["id"], "user_name": owner,
                                  "ts": dm_slack_ts(pre_start + timedelta(minutes=9)),
                                  "text": f"Moving this to #incidents, see {canonical_permalink}"})
        # stash the team thread as an incident-shaped noise thread so slack.py serves it generically
        noise_slack.append({"channel": team_chan, "user": team_thread_msgs[0]["user"], "user_name": owner,
                             "ts": pre_ts, "thread_ts": pre_ts,
                             "text": team_thread_msgs[0]["text"]})
        for m in team_thread_msgs[1:]:
            noise_slack.append({"channel": team_chan, "user": m["user"], "user_name": m["user_name"],
                                 "ts": m["ts"], "thread_ts": pre_ts, "text": m["text"]})

        inc["escalation"] = {"team_channel": team_chan, "team_thread_ts": pre_ts, "permalink": team_permalink}
        inc["slack"]["escalated_from_permalink"] = team_permalink
        add_msg(inc, owner, t0 + timedelta(minutes=4),
                f"Continuing from {team_chan}, see {team_permalink}. Pager went off for {svc_name}: {cls}. Ticket {inc['jira']['key']} incoming.")
        add_msg(inc, owner, t0 + timedelta(minutes=10), f"Seeing `{inc['error_sig']}` in logs, e.g. trace_id={inc['trace_ids'][0]} on {inc['pods'][0]}")
        reporter = inc["jira"]["reporter"]
        add_msg(inc, reporter, t0 + timedelta(minutes=13), f"Also trace_id={inc['trace_ids'][1]} from a customer report, same error.")
        add_msg(inc, owner, t0 + timedelta(minutes=25), f"Suspect {inc['commit'][:10]} touched {inc['function']}() yesterday. Rolling back.")
        add_msg(inc, owner, t0 + timedelta(minutes=58), f"Rollback done, error rate back to baseline. Will write up in {inc['jira']['key']}.")
        inc["confluence"]["page_id"] = runbook_id[svc_name]
        add_logs(inc, inc["trace_ids"], inc["pods"])
        new_incidents.append(inc)
        escalation_ids.append(inc["id"])

    # =================================================================== 4. deploy-preceded incidents
    deploy_specs = [("auth-service", 1), ("search-api", 0), ("billing-service", 1)]
    deploys = []
    for di, (svc_name, err_idx) in enumerate(deploy_specs):
        cls, msg, func = errors_by_service[svc_name][err_idx]
        day = 296 + di * 4
        t0 = BASE + timedelta(days=day, hours=rnd.randint(0, 8), minutes=rnd.randint(0, 59))
        inc = make_incident(svc_name, cls, msg, func, t0)
        deploy_time = t0 - timedelta(minutes=rnd.randint(5, 25))
        deploy_ts = dm_slack_ts(deploy_time)
        deploy_channel = channels["#deploys"]["id"]
        dep_id = f"DEP-{di + 1:03d}"
        message = f"Deployed {svc_name}@{_sha10(inc['id'])} to prod"
        deploy_permalink = permalink(deploy_channel, deploy_ts)
        deploys.append({"id": dep_id, "service": svc_name, "sha": inc["commit"], "at": _ts(deploy_time),
                         "channel": "#deploys", "thread_ts": deploy_ts, "message": message, "permalink": deploy_permalink})
        inc["deploy_id"] = dep_id
        owner = inc["jira"]["assignee"]
        canonical_ts = dm_slack_ts(t0 + timedelta(minutes=4))
        inc["slack"]["thread_ts"] = canonical_ts
        add_msg(inc, owner, t0 + timedelta(minutes=4),
                f"Pager went off for {svc_name}: {cls}. Deploy {deploy_permalink} looks suspect. Ticket {inc['jira']['key']} incoming.")
        add_msg(inc, owner, t0 + timedelta(minutes=8), f"Confirmed: errors started right after {deploy_permalink}. Seeing trace_id={inc['trace_ids'][0]}.")
        reporter = inc["jira"]["reporter"]
        add_msg(inc, reporter, t0 + timedelta(minutes=11), f"Also trace_id={inc['trace_ids'][1]} from a customer report.")
        add_msg(inc, owner, t0 + timedelta(minutes=22), f"Rolling back {_sha10(inc['id'])}.")
        add_msg(inc, owner, t0 + timedelta(minutes=50), f"Rollback done, error rate back to baseline. Will write up in {inc['jira']['key']}.")
        inc["confluence"]["page_id"] = runbook_id[svc_name]
        add_logs(inc, inc["trace_ids"], inc["pods"])
        new_incidents.append(inc)
    world["deploys"] = deploys
    deploy_incident_ids = [i["id"] for i in new_incidents if "deploy_id" in i]

    # =================================================================== 5. still-open incidents
    still_open_specs = [("event-bus", 0), ("webhook-dispatcher", 1), ("stream-processor", 0), ("indexer", 0)]
    still_open_ids = []
    for si, (svc_name, err_idx) in enumerate(still_open_specs):
        cls, msg, func = errors_by_service[svc_name][err_idx]
        day = 310 + si * 3
        t0 = BASE + timedelta(days=day, hours=rnd.randint(0, 8), minutes=rnd.randint(0, 59))
        inc = make_incident(svc_name, cls, msg, func, t0, resolved=False)
        finalize(inc, resolved=False)
        new_incidents.append(inc)
        still_open_ids.append(inc["id"])

    # =================================================================== overlay: long threads with red herrings
    by_id = {i["id"]: i for i in new_incidents}
    by_service = {}
    for i in new_incidents:
        by_service.setdefault(i["service"], []).append(i["id"])
    herring_targets = (cascade_downstreams_for_herrings[:2] + repeat_second_ids[:2]
                       + escalation_ids[4:6] + deploy_incident_ids[:1] + still_open_ids[:1])
    all_new_ids = [i["id"] for i in new_incidents]
    for target_id in herring_targets:
        inc = by_id[target_id]
        siblings = [x for x in by_service.get(inc["service"], []) if x != target_id]
        other_id = siblings[0] if siblings else rnd.choice([x for x in all_new_ids if x != target_id])
        other = by_id[other_id]
        foreign_id = rnd.choice([x for x in all_new_ids if x != target_id and by_id[x]["service"] != inc["service"]])
        foreign_tid = by_id[foreign_id]["trace_ids"][0]
        t0 = datetime.fromisoformat(inc["started_at"].replace("Z", "+00:00"))
        wrong_when = t0 + timedelta(minutes=15)
        retract_when = t0 + timedelta(minutes=18)
        foreign_when = t0 + timedelta(minutes=16)
        owner = inc["jira"]["assignee"]
        inc["slack"]["messages"].append({"user": people[owner]["id"], "user_name": owner,
                                          "ts": dm_slack_ts(wrong_when),
                                          "text": f"Wait, might be {{{{WRONG_SHA:{other['id']}}}}} -- that also touched {inc['service']} recently."})
        inc["slack"]["messages"].append({"user": people[owner]["id"], "user_name": owner,
                                          "ts": dm_slack_ts(foreign_when),
                                          "text": f"Also someone pasted trace_id={foreign_tid} in support, not sure it's related."})
        inc["slack"]["messages"].append({"user": people[owner]["id"], "user_name": owner,
                                          "ts": dm_slack_ts(retract_when),
                                          "text": "False alarm on that other commit, that's unrelated to this one."})
        inc["slack"]["messages"].sort(key=lambda m: float(m["ts"]))

    # =================================================================== overlay: cross-referencing jira comments
    def slack_permalink_for(inc: dict) -> str:
        return permalink(channels[inc["slack"]["channel"]]["id"], inc["slack"]["thread_ts"])

    comment_targets = cascade_upstream_ids + repeat_second_ids + escalation_ids[:4]
    for cid in comment_targets:
        inc = by_id.get(cid) or next(i for i in incidents if i["id"] == cid)
        owner = inc["jira"]["assignee"]
        reporter = inc["jira"]["reporter"]
        comments = [
            {"author": reporter, "created": _ts(datetime.fromisoformat(inc["started_at"].replace("Z", "+00:00")) + timedelta(minutes=15)),
             "body": f"Customers are reporting this too, see {slack_permalink_for(inc)}"},
        ]
        if inc.get("parent_incident"):
            comments.append({"author": owner, "created": inc["jira"]["updated"],
                              "body": f"Related to {inc['parent_incident']}; blocked by the upstream fix, see issuelinks."})
        elif inc.get("child_incidents"):
            for child_key in inc["jira"].get("issuelinks", []):
                comments.append({"author": owner, "created": inc["jira"]["updated"],
                                  "body": f"This is causing downstream failures, related to {child_key['key']}."})
        elif inc.get("repeat_of"):
            comments.append({"author": owner, "created": inc["jira"]["updated"],
                              "body": f"Duplicate root cause of {inc['repeat_of']}'s ticket -- see the postmortem once it's up."})
        else:
            other = rnd.choice([x for x in comment_targets if x != cid])
            comments.append({"author": owner, "created": inc["jira"]["updated"],
                              "body": f"Not related to {other}, different root cause, but similar symptoms."})
        comments.append({"author": owner, "created": inc["jira"]["updated"], "body": f"Resolved by rolling back {_sha10(inc['id'])}."})
        inc["jira"]["comments"] = comments

    # ---------------------------------------------------------------- append incidents
    incidents.extend(new_incidents)

    # =================================================================== confluence: runbooks
    for svc_name in new_service_names:
        svc = services[svc_name]
        team = svc["team"]
        errs = errors_by_service[svc_name]
        body = [f"h1. {svc_name} runbook", "",
                f"Owned by team {team} ({', '.join(svc['owners'])}). Alerts go to {svc['slack_channel']}; incidents are discussed in {svc['incident_channel']}.",
                "", "h2. Known failure modes", ""]
        for cls, msg, func in errs:
            body += [f"h3. {cls}", f"Symptom: log lines containing '{cls}: {msg}' from {func}().",
                     f"Check: error rate panel for service={svc_name}; search logs for the trace_id in the alert.",
                     f"Remediation: roll back the latest deploy of {svc_name}; if persists, page {team} on-call.", ""]
        body += ["h2. Dashboards", f"Chronosphere: rate(http_requests_total{{service=\"{svc_name}\",code=~\"5..\"}}[5m])", ""]
        pages.append({"id": runbook_id[svc_name], "title": f"{svc_name} runbook", "space": team.upper(),
                      "labels": ["runbook", svc_name], "created": _ts(BASE - timedelta(days=40)),
                      "last_modified": _ts(BASE + timedelta(days=300)),
                      "links": [arch_id[team]], "parent_id": None, "body": "\n".join(body)})

    # =================================================================== confluence: on-call guides for new teams
    for team in NEW_TEAMS:
        pages.append({"id": oncall_guide_id[team], "title": f"Team {team} on-call guide", "space": team.upper(),
                      "labels": ["oncall"], "created": _ts(BASE - timedelta(days=30)), "last_modified": _ts(BASE + timedelta(days=300)),
                      "links": [arch_id[team]], "parent_id": None,
                      "body": f"h1. Team {team} on-call\n\nRotation in world.oncall_rotations['{team}']. Escalate in #team-{team}. Services: "
                              + ", ".join(services_by_team[team])})

    # =================================================================== confluence: architecture pages
    for team in NEW_TEAMS:
        runbooks = [runbook_id[s] for s in services_by_team[team]]
        pages.append({"id": arch_id[team], "title": f"{team.title()} architecture", "space": team.upper(),
                      "labels": ["architecture", team], "created": _ts(BASE - timedelta(days=50)),
                      "last_modified": _ts(BASE + timedelta(days=300)),
                      "links": runbooks + [oncall_guide_id[team]], "parent_id": None,
                      "body": f"h1. {team.title()} architecture\n\nServices: " + ", ".join(services_by_team[team])
                              + f"\n\nSee runbooks for each service and the {team} on-call guide."})
    # link (never mutate) one original-4 runbook from each cross-team page, so the whole entity graph --
    # the untouched original incidents included -- is one connected component, not two disjoint halves.
    old_runbook_id = {p["title"]: p["id"] for p in pages if p["title"].endswith(" runbook")}
    cross1_links = [runbook_id["billing-service"], runbook_id["invoicing"], runbook_id["auth-service"],
                     old_runbook_id["payments-api runbook"]]
    cross2_links = [runbook_id["event-bus"], runbook_id["webhook-dispatcher"], runbook_id["stream-processor"],
                     runbook_id["search-api"], old_runbook_id["notifier runbook"]]
    pages.append({"id": arch_id["cross1"], "title": "Payments & billing integration architecture", "space": "BILLING",
                  "labels": ["architecture", "cross-team"], "created": _ts(BASE - timedelta(days=45)),
                  "last_modified": _ts(BASE + timedelta(days=300)), "links": cross1_links, "parent_id": None,
                  "body": "h1. Payments & billing integration architecture\n\nHow billing-service and invoicing authenticate "
                          "against auth-service and reconcile plans."})
    pages.append({"id": arch_id["cross2"], "title": "Event-driven platform architecture", "space": "EVENTBUS",
                  "labels": ["architecture", "cross-team"], "created": _ts(BASE - timedelta(days=45)),
                  "last_modified": _ts(BASE + timedelta(days=300)), "links": cross2_links, "parent_id": None,
                  "body": "h1. Event-driven platform architecture\n\nHow event-bus, webhook-dispatcher and stream-processor "
                          "feed search-api's index pipeline."})

    # =================================================================== confluence: postmortems (link into architecture)
    for pm, (svc_name, _err_idx) in zip(postmortem_pages, repeat_specs):
        team = services[svc_name]["team"]
        arch_id[team]  # no-op, documents the link target
        pages.append(pm)
    for pm, (svc_name, _err_idx) in zip(postmortem_pages, repeat_specs):
        team = services[svc_name]["team"]
        for p in pages:
            if p["id"] == arch_id[team] and pm["id"] not in p["links"]:
                p["links"].append(pm["id"])

    # =================================================================== confluence: filler / noise pages
    for title, pid in zip(FILLER_PAGE_TITLES, filler_id):
        space = rnd.choice(NEW_TEAMS).upper()
        pages.append({"id": pid, "title": title, "space": space, "labels": ["reference"],
                      "created": _ts(BASE - timedelta(days=rnd.randint(20, 200))),
                      "last_modified": _ts(BASE + timedelta(days=rnd.randint(0, 300))),
                      "links": [], "parent_id": None,
                      "body": f"h1. {title}\n\nGeneral reference documentation, not tied to a specific incident."})

    # =================================================================== noise: incident-adjacent chatter
    noise_channel_pool = [c for c in channels if c not in ("#incidents",)]
    grouped = [2, 3, 2, 3]
    phrase_iter = iter(NOISE_PHRASES)
    idx = 0
    for size in grouped:
        cname = rnd.choice(noise_channel_pool)
        who = rnd.choice(list(TEAM_ROSTER_NAMES[rnd.choice(NEW_TEAMS)]))
        when = BASE + timedelta(days=rnd.randint(40, 360), hours=rnd.randint(8, 18), minutes=rnd.randint(0, 59))
        root_ts = dm_slack_ts(when)
        for j in range(size):
            phrase = next(phrase_iter, rnd.choice(NOISE_PHRASES))
            m_ts = root_ts if j == 0 else dm_slack_ts(when + timedelta(minutes=3 * j))
            noise_slack.append({"channel": cname, "user": people[who]["id"], "user_name": who,
                                 "ts": m_ts, "thread_ts": root_ts, "text": phrase})
        idx += size
