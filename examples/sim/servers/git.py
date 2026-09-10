"""Simulated git server: the built-in git server (crystal/servers/git.py) pinned to the sim repo."""
from pathlib import Path

from crystal.servers.git import make_server

REPO = Path(__file__).resolve().parent.parent / "repo"
mcp = make_server(REPO, name="sim-git")

if __name__ == "__main__":
    mcp.run()
