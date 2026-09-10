"""Build examples/sim/catalog.yaml (the entity catalog: the foreign-key hub) from the sim world. For a real
workspace this is dumped from the real MCP servers (Jira projects/components, Slack channels, PagerDuty services,
Prometheus label values, CODEOWNERS) into <state dir>/catalog.yaml."""
import json, yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent
w = json.loads((ROOT / "data" / "world.json").read_text())
services = {}
for name, s in w["services"].items():
    services[name] = {
        "aliases": list(dict.fromkeys([name.replace("-", "_"), s["repo_path"].split("/")[-1], *s.get("aliases", [])])),
        "team": s["team"], "owners": s["owners"], "jira_project": s["jira_project"],
        "slack_channel": s["slack_channel"].lstrip("#"), "incident_channel": s["incident_channel"].lstrip("#"),
        "pagerduty_service_id": s["pagerduty_service_id"], "repo_path": s["repo_path"],
        "prom_selector": f'service="{name}"', "logz_type": s["logz_type"], "confluence_space": s["team"].upper(),
    }
teams = {t: {"members": m, "slack_channel": f"team-{t}", "confluence_space": t.upper()} for t, m in w["teams"].items()}
channels = {c["name"]: {"id": c["id"]} for c in w["channels"].values()}
channels.update({f"@{n}": {"id": d["id"], "is_im": True, "user": n} for n, d in w.get("dms", {}).items()})
people = {p["name"]: {"slack_id": p["id"], "team": p["team"]} for p in w["people"].values()}
out = {"service": services, "team": teams, "slack_channel": channels, "person": people}
(ROOT / "catalog.yaml").write_text(yaml.safe_dump(out, sort_keys=False))
print("catalog:", {k: len(v) for k, v in out.items()})
