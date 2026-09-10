"""Simulated codebase server: the built-in code server (crystal/servers/code.py) pinned to the sim repo."""
from pathlib import Path

from crystal.servers.code import make_server

REPO = Path(__file__).resolve().parent.parent / "repo"
mcp = make_server(REPO, name="sim-code", default_glob="**/*.py")

if __name__ == "__main__":
    mcp.run()
