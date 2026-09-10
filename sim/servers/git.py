"""Simulated git server: the generic git server (crystal/servers/git.py) pinned to the sim repo."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crystal.servers.git import make_server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent / "repo"
mcp = make_server(REPO, name="sim-git")

if __name__ == "__main__":
    mcp.run()
