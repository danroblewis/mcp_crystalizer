"""crystal: record agent tool-call traces, crystallize them into flows, run flows with no AI.

This is the package behind the `mcp-explorer` command. Nothing here assumes it runs inside its own source checkout:
the workspace is the current directory (crystal/workspace.py) and every bit of state lives under
$MCP_EXPLORER_HOME (crystal/state.py).
"""
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
