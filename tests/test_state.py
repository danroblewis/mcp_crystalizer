"""The state dir ($MCP_EXPLORER_HOME/workspaces/<slug>/): slugging, first-sight setup, seeding, isolation between
workspaces, and the workspaces listing. Nothing is ever written into a workspace directory or into this repo."""
import json
from pathlib import Path

import pytest

from crystal import state as state_mod
from crystal import workspace as ws_mod
from crystal.flow.runner import list_flows, load_flow
from crystal.trace.store import load_sessions
from tests.conftest import SIM


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(h))
    return h


def test_home_default_and_override(home, monkeypatch):
    assert state_mod.home() == home
    monkeypatch.delenv("MCP_EXPLORER_HOME")
    assert state_mod.home() == Path("~/.mcp-explorer").expanduser()


def test_slugging():
    a = Path("/srv/repos/api")
    assert state_mod.slug_of(a) == "api-" + __import__("hashlib").sha1(str(a).encode()).hexdigest()[:8]
    assert state_mod.slug_of(Path("/srv/other/api")) != state_mod.slug_of(a)
    assert state_mod.slug_of(Path("/")).startswith("workspace-")


def test_ensure_creates_layout_and_workspace_json(home, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    st = state_mod.state_for(root).ensure()
    assert st.dir == home / "workspaces" / state_mod.slug_of(root)
    assert all(d.is_dir() for d in (st.flows, st.runs, st.traces, st.cassettes))
    info = json.loads(st.workspace_json.read_text())
    assert info["root"] == str(root) and info["name"] == "proj" and "first_seen" in info and "remote" in info
    assert sorted(p.name for p in root.iterdir()) == []            # nothing written into the workspace
    first = st.workspace_json.read_text()
    st.ensure()
    assert st.workspace_json.read_text() == first                  # idempotent


def test_seed_from_a_directory_and_never_overwrite(home, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    st = state_mod.state_for(root).ensure()
    counts = state_mod.seed(st, SIM)
    assert counts["flows"] >= 8 and counts["traces"] >= 50 and counts["catalog"] == 1
    assert (st.flows / "investigate-jira-ticket.yaml").exists() and (st.traces / "runs").is_dir() and st.catalog.exists()
    (st.flows / "investigate-jira-ticket.yaml").write_text("name: edited\n")
    again = state_mod.seed(st, SIM)
    assert again == {"flows": 0, "traces": 0, "catalog": 0} and (st.flows / "investigate-jira-ticket.yaml").read_text() == "name: edited\n"
    state_mod.seed(st, SIM, overwrite=True)
    assert "name: investigate-jira-ticket" in (st.flows / "investigate-jira-ticket.yaml").read_text()
    with pytest.raises(FileNotFoundError):
        state_mod.seed(st, tmp_path / "missing")


def test_workspace_seed_dir_is_copied_on_first_use_only(home, tmp_path):
    root = tmp_path / "proj"
    (root / ".mcp-explorer" / "flows").mkdir(parents=True)
    (root / ".mcp-explorer" / "flows" / "f.yaml").write_text("name: f\nsteps: []\n")
    (root / ".mcp-explorer" / "catalog.yaml").write_text("service: {}\n")
    st = state_mod.state_for(root).ensure()
    assert (st.flows / "f.yaml").exists() and st.catalog.exists() and (st.dir / state_mod.SEED_MARKER).exists()
    (st.flows / "f.yaml").unlink()
    st.ensure()
    assert not (st.flows / "f.yaml").exists()                      # seeded once; a deleted flow stays deleted


def test_the_sim_example_seeds_itself(home, monkeypatch):
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM))
    st = ws_mod.current().state.ensure()
    names = {f["name"] for f in list_flows()}
    assert {"investigate-jira-ticket", "investigate-slack-thread", "investigate-slack-dm", "induced-jira-ticket-all"} <= names
    assert load_flow("investigate-jira-ticket")["_path"].startswith(str(st.flows))
    assert len(load_sessions(trigger="jira_issue")) == 17 and st.catalog.exists()


def test_workspaces_are_isolated(home, tmp_path, monkeypatch):
    """Two workspaces never see each other's flows, runs or traces; a fresh directory looks like a fresh install."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(a))
    sa = ws_mod.current().state.ensure()
    state_mod.seed(sa, SIM)
    assert len(list_flows()) >= 8 and len(load_sessions()) >= 50
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(b))
    sb = ws_mod.current().state.ensure()
    assert list_flows() == [] and load_sessions() == [] and not sb.catalog.exists()
    from crystal.extract.catalog import load_catalog
    assert load_catalog() == {}
    with pytest.raises(FileNotFoundError):
        load_flow("investigate-jira-ticket")
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(a))
    assert len(list_flows()) >= 8


def test_list_workspaces(home, tmp_path, monkeypatch):
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM))
    assert state_mod.list_workspaces() == []
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir(), b.mkdir()
    state_mod.state_for(a).ensure()
    sb = state_mod.state_for(b).ensure()
    state_mod.seed(sb, SIM)
    rows = {r["name"]: r for r in state_mod.list_workspaces()}
    assert set(rows) == {"alpha", "beta"} and rows["beta"]["flows"] >= 8 and rows["alpha"]["flows"] == 0
    assert rows["alpha"]["root"] == str(a) and rows["alpha"]["exists"]
    from crystal.cli import main
    assert main(["workspaces"]) == 0


def test_cli_seed_command(home, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM))   # main() activates a workspace via os.environ; restore it
    from crystal.cli import main
    root = tmp_path / "proj"
    root.mkdir()
    assert main(["--workspace", str(root), "seed", "--from", str(SIM)]) == 0
    out = capsys.readouterr().out
    assert "seeded" in out and "flows" in out
    assert (state_mod.state_for(root.resolve()).flows / "investigate-jira-ticket.yaml").exists()
    assert main(["--workspace", str(root), "seed"]) == 1
