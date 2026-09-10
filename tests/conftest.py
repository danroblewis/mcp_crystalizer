import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The test suite creates a ServerPool per test/module (tests/test_flow_jira.py, test_flows_slack.py,
# test_inducer.py, test_lifecycle.py, test_sim_scenarios.py, and sim/servers/flows.py's run_flow for
# test_author.py), and each pool spawning the 8 simulated MCP servers as stdio subprocesses (each importing
# mcp/pydantic) dominates the suite's runtime. Default to the in-process transport (crystal/mcp_client.py:
# ServerPool connects to the sim servers' MCPServer instances directly over in-memory streams, no subprocess)
# for the whole run; real stdio (used by Claude Code via .mcp.json and by the UI) is untouched since it isn't
# driven by this env var. `CRYSTAL_INPROCESS=0 uv run pytest` (or any ServerPool(inprocess=False)) still runs
# the real stdio path -- see tests/test_transport_parity.py, which checks the two transports agree.
os.environ.setdefault("CRYSTAL_INPROCESS", "1")

# The effective server registry layers ~/.mcp.json over servers.yaml (crystal/registry.py). The suite must see the
# project's servers only, whatever the developer keeps in their home: point the user-level file at nothing. Tests
# of that layer set $CRYSTAL_USER_MCP (or delete it and monkeypatch Path.home) themselves. The workspace is the
# project (the sim) unless a test activates another one.
os.environ.setdefault("CRYSTAL_USER_MCP", str(Path(__file__).resolve().parent / "no-user-mcp.json"))
os.environ.pop("CRYSTAL_WORKSPACE", None)
