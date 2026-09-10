"""Sim MCP servers.

The server modules (jira.py, slack.py, ...) use bare `from common import ...` so they work when launched directly as
scripts (`python servers/jira.py` from examples/sim, in stdio mode - the script's own directory is on sys.path) and
when the in-process transport loads them by path (crystal/registry.py: load_script does the same). Importing them
as a package (`examples.sim.servers.jira`, in tests) goes through here, which puts this directory on sys.path too.
"""
import sys
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)
