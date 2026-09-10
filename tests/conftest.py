"""Test-suite environment: a temporary $MCP_EXPLORER_HOME seeded from examples/sim, the sim as the workspace, the
in-process MCP transport, and no dependence on the developer's home directory.

Set at import time (module-level fixtures in several test files load flows and traces before any fixture runs):

  MCP_EXPLORER_HOME       a fresh temp dir per session; the sim workspace's state dir in it is seeded from
                          examples/sim (flows/, traces/, catalog.yaml) exactly as `mcp-explorer seed --from
                          examples/sim` would. Removed at the end of the session. Tests never write anywhere else.
  CRYSTAL_WORKSPACE       examples/sim: its .mcp.json declares the simulated jira/slack/... servers and pins code/git
                          to the sim repo. Tests that need another workspace set the variable themselves.
  CRYSTAL_INPROCESS=1     ServerPool connects to python servers in-process (crystal/mcp_client.py) instead of
                          spawning 9 stdio subprocesses per pool; tests/test_transport_parity.py checks the two
                          transports agree.
  CRYSTAL_USER_MCP, CRYSTAL_CLAUDE_CONFIG, CRYSTAL_CLAUDE_SETTINGS
                          point ~/.mcp.json, ~/.claude.json and ~/.claude/settings.json at files that do not exist,
                          so whatever the developer keeps in their home never reaches the suite.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SIM = REPO / "examples" / "sim"
sys.path.insert(0, str(REPO))

_home = Path(tempfile.mkdtemp(prefix="mcp-explorer-tests-"))
os.environ["MCP_EXPLORER_HOME"] = str(_home)
os.environ["CRYSTAL_WORKSPACE"] = str(SIM)
os.environ.setdefault("CRYSTAL_INPROCESS", "1")
for var in ("CRYSTAL_USER_MCP", "CRYSTAL_CLAUDE_CONFIG", "CRYSTAL_CLAUDE_SETTINGS"):
    os.environ[var] = str(REPO / "tests" / "no-such-file.json")
os.environ.pop("CRYSTAL_LIFECYCLE_DB", None)

from crystal import state as state_mod  # noqa: E402

SIM_STATE = state_mod.state_for(SIM).ensure()      # seeds from examples/sim/.mcp-explorer (links to flows/, traces/, catalog.yaml)
assert (SIM_STATE.flows / "investigate-jira-ticket.yaml").exists(), "seeding the sim workspace failed"
atexit.register(shutil.rmtree, _home, True)


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """A private $MCP_EXPLORER_HOME for one test, with the sim workspace seeded (flows, traces, catalog) and an
    otherwise empty state: for tests that save runs, feedback, versions or lifecycle events."""
    from crystal.flow import lifecycle as lc_mod
    home = tmp_path / "home"
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(home))
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM))
    monkeypatch.delenv("CRYSTAL_LIFECYCLE_DB", raising=False)
    monkeypatch.setattr(lc_mod, "_default", None)
    st = state_mod.state_for(SIM).ensure()
    return st
