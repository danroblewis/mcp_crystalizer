"""The packaged command: `uvx --from . mcp-explorer --help` works from a checkout, and serving an empty directory
shows the getting-started panel with none of the sim's flows."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.conftest import REPO


@pytest.mark.skipif(not shutil.which("uvx"), reason="uvx not installed")
def test_uvx_from_checkout_help():
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV",)}
    r = subprocess.run(["uvx", "--from", str(REPO), "mcp-explorer", "--help"], capture_output=True, text=True, timeout=600, env=env, cwd=REPO)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "mcp-explorer" in r.stdout and "serve" in r.stdout and "install-hook" in r.stdout


def test_uv_run_help_and_unknown_command():
    r = subprocess.run(["uv", "run", "mcp-explorer", "--help"], capture_output=True, text=True, timeout=300, cwd=REPO)
    assert r.returncode == 0 and "workspaces" in r.stdout
    from crystal.cli import main
    assert main(["no-such-command"]) == 1


def test_serve_an_empty_directory_shows_getting_started_and_no_sim_flows(tmp_path, monkeypatch):
    import crystal.app.main as web
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    empty = tmp_path / "empty-project"
    empty.mkdir()
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(empty))
    if hasattr(web.app.state, "workspace"):
        monkeypatch.delattr(web.app.state, "workspace")
    body = TestClient(web.app).get("/").text
    assert 'id="getting-started"' in body and "install-hook" in body and "mcp-explorer record" in body and "induce" in body
    assert "investigate-jira-ticket" not in body and "Investigate a Jira ticket" not in body
    assert "empty-project" in body and str(empty.resolve()) in body
    assert TestClient(web.app).get("/traces").status_code == 200
    assert sorted(p.name for p in empty.iterdir()) == []         # the workspace directory is untouched


def test_serve_the_sim_lists_its_flows(monkeypatch):
    import crystal.app.main as web
    if hasattr(web.app.state, "workspace"):
        monkeypatch.delattr(web.app.state, "workspace")
    body = TestClient(web.app).get("/").text
    assert 'id="getting-started"' not in body and "Investigate a Jira ticket" in body and ">sim<" in body


def test_serve_command_parses_port_and_starts_uvicorn(monkeypatch, capsys):
    from crystal import cli
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    assert cli.main(["serve", "--port", "9001"]) == 0
    assert seen["app"] == "crystal.app.main:app" and seen["port"] == 9001 and seen["host"] == "127.0.0.1"
    assert "http://127.0.0.1:9001" in capsys.readouterr().out
    assert cli.main(["--port", "9002"]) == 0 and seen["port"] == 9002       # no subcommand = serve
