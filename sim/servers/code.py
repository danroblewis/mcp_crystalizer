"""Simulated codebase server: the generic code server (crystal/servers/code.py) pinned to the sim repo."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crystal.servers.code import make_server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent / "repo"
mcp = make_server(REPO, name="sim-code", default_glob="**/*.py")

if __name__ == "__main__":
    mcp.run()
