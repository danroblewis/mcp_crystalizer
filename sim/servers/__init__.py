"""Sim MCP servers package.

The server modules (jira.py, slack.py, ...) use bare `from common import ...` so they still work
when launched directly as scripts (`python sim/servers/jira.py`, in stdio mode - the script's own
directory is put on sys.path automatically). To let the same modules be imported as
`sim.servers.jira` (for the in-process transport), put this directory on sys.path here so `common`
resolves either way.
"""
import sys
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)
