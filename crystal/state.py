"""Where state lives: $MCP_EXPLORER_HOME (default ~/.mcp-explorer), never in a workspace and never in this package.

    $MCP_EXPLORER_HOME/
      workspaces/<slug>/          one directory per workspace this tool has been pointed at
        workspace.json            {root, name, remote, first_seen}
        flows/*.yaml              the workspace's crystallized flows (drafts, versions, promoted flows)
        runs/*.json               saved flow runs (what the dossier shows)
        traces/*.jsonl            recorded agent sessions (the hook, `record`, the scripted agent); traces/runs/ holds
                                  the run records that recorded sessions refer to
        cassettes/<flow>.json     recorded responses for `mcp-explorer test`
        dataflow/<trace>.json     each trace's mined dataflow edges, keyed by its size and mtime (a cache the
                                  candidate miner rebuilds on demand; safe to delete)
        dataflow/index.json       ONE file: every trace reduced to what mining reads off it (size, mtime, session
                                  id, prompt, the server.tool of each call), rebuilt incrementally so listing
                                  candidates never re-parses the workspace's traces (also safe to delete)
        catalog.yaml              the entity catalog (the foreign-key hub the extractors use)
        lifecycle.sqlite          promotion state and counters (crystal/flow/lifecycle.py)
        feedback.jsonl            the "this didn't help" queue (`mcp-explorer repair`)
        records/*.json            Claude Code runs launched from the UI (/record): status, cost, session (crystal/app/routes_record.py)

The slug is `<dirname>-<8 hex of sha1(absolute path)>`, so two checkouts called `api` never share flows and a
workspace keeps its state when the tool is upgraded. Workspaces are isolated: nothing here ever looks across slugs
except `list_workspaces()`.

Seeding: a workspace may ship starting data in `<workspace>/.mcp-explorer/` (flows/, traces/, catalog.yaml; the sim
example does). It is copied into the state dir the first time the workspace is used (`ensure()`), and
`mcp-explorer seed --from <dir>` copies the same layout from any directory on demand. Seeding never overwrites a
file that already exists in the state dir, and never deletes anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HOME_ENV = "MCP_EXPLORER_HOME"
DEFAULT_HOME = "~/.mcp-explorer"
SEED_DIRNAME = ".mcp-explorer"       # inside a workspace: seed data copied into the state dir on first use
SEED_MARKER = ".seeded"


def home() -> Path:
    raw = os.environ.get(HOME_ENV, "").strip()
    return Path(raw or DEFAULT_HOME).expanduser()


def slug_of(root: Path) -> str:
    """`<dirname>-<8 hex of sha1(abs path)>`: readable, unique per absolute path, stable across runs."""
    root = Path(root)
    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "workspace"
    return f"{name}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class StateDir:
    """The state directory of one workspace. Pure path arithmetic until `ensure()` is called."""
    root: Path          # the workspace root (absolute)
    dir: Path           # $MCP_EXPLORER_HOME/workspaces/<slug>

    @property
    def slug(self) -> str:
        return self.dir.name

    @property
    def flows(self) -> Path:
        return self.dir / "flows"

    @property
    def runs(self) -> Path:
        return self.dir / "runs"

    @property
    def traces(self) -> Path:
        return self.dir / "traces"

    @property
    def cassettes(self) -> Path:
        return self.dir / "cassettes"

    @property
    def dataflow(self) -> Path:
        return self.dir / "dataflow"

    @property
    def catalog(self) -> Path:
        return self.dir / "catalog.yaml"

    @property
    def lifecycle_db(self) -> Path:
        return self.dir / "lifecycle.sqlite"

    @property
    def feedback(self) -> Path:
        return self.dir / "feedback.jsonl"

    @property
    def workspace_json(self) -> Path:
        return self.dir / "workspace.json"

    def exists(self) -> bool:
        return self.workspace_json.exists()

    def info(self) -> dict:
        if self.workspace_json.exists():
            try:
                return json.loads(self.workspace_json.read_text())
            except json.JSONDecodeError:
                pass
        return {"root": str(self.root), "name": self.root.name, "slug": self.slug}

    def ensure(self, remote: str | None = None) -> "StateDir":
        """Create the directory, write workspace.json on first sight, and seed from <root>/.mcp-explorer/ once."""
        self.dir.mkdir(parents=True, exist_ok=True)
        for d in (self.flows, self.runs, self.traces, self.cassettes):
            d.mkdir(exist_ok=True)
        if not self.workspace_json.exists():
            info = {"root": str(self.root), "name": self.root.name, "slug": self.slug,
                    "remote": remote if remote is not None else _git_remote(self.root),
                    "first_seen": datetime.now(timezone.utc).isoformat()}
            self.workspace_json.write_text(json.dumps(info, indent=2) + "\n")
        seed_src = self.root / SEED_DIRNAME
        if seed_src.is_dir() and not (self.dir / SEED_MARKER).exists():
            seed(self, seed_src)
            (self.dir / SEED_MARKER).write_text(datetime.now(timezone.utc).isoformat() + "\n")
        return self

    def __str__(self) -> str:
        return str(self.dir)


def state_for(root: Path) -> StateDir:
    root = Path(root)
    return StateDir(root=root, dir=home() / "workspaces" / slug_of(root))


def current() -> StateDir:
    """The state dir of the current workspace ($CRYSTAL_WORKSPACE, else the current directory)."""
    from crystal.workspace import current as current_workspace
    return current_workspace().state


# ---------------------------------------------------------------- per-kind lookups (what the modules call)
def flow_dir() -> Path:
    return current().flows


def run_dir() -> Path:
    return current().runs


def trace_dir() -> Path:
    return current().traces


def cassette_dir() -> Path:
    return current().cassettes


def catalog_path() -> Path:
    return current().catalog


def lifecycle_db() -> Path:
    return current().lifecycle_db


def feedback_path() -> Path:
    return current().feedback


# ---------------------------------------------------------------- seeding
def seed(state: StateDir, src: Path, overwrite: bool = False) -> dict[str, int]:
    """Copy `src/flows/*.yaml`, `src/traces/**` and `src/catalog.yaml` into the state dir. Existing files are kept
    unless `overwrite`. Returns counts per kind."""
    src = Path(src)
    if not src.is_dir():
        raise FileNotFoundError(f"seed source {src} is not a directory")
    state.dir.mkdir(parents=True, exist_ok=True)
    counts = {"flows": 0, "traces": 0, "catalog": 0}
    flows = src / "flows"
    if flows.is_dir():
        state.flows.mkdir(exist_ok=True)
        for p in sorted(flows.glob("*.yaml")):
            if _copy(p, state.flows / p.name, overwrite):
                counts["flows"] += 1
    traces = src / "traces"
    if traces.is_dir():
        for p in sorted(traces.rglob("*")):
            if p.is_file() and p.suffix in (".jsonl", ".json"):
                dest = state.traces / p.relative_to(traces)
                if _copy(p, dest, overwrite):
                    counts["traces"] += 1
    catalog = src / "catalog.yaml"
    if catalog.is_file() and _copy(catalog, state.catalog, overwrite):
        counts["catalog"] = 1
    return counts


def _copy(src: Path, dest: Path, overwrite: bool) -> bool:
    if dest.exists() and not overwrite:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)   # follows symlinks: a seed dir may be made of links into the example
    return True


# ---------------------------------------------------------------- listing
def list_workspaces() -> list[dict]:
    """Every workspace with a state dir under $MCP_EXPLORER_HOME: workspace.json plus counts, newest first."""
    base = home() / "workspaces"
    out = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        st = StateDir(root=Path("?"), dir=d)
        if not st.workspace_json.exists():
            continue
        info = st.info()
        info.update(slug=d.name, state_dir=str(d), exists=Path(info.get("root", "?")).is_dir(),
                    flows=len(list(st.flows.glob("*.yaml"))) if st.flows.is_dir() else 0,
                    runs=len(list(st.runs.glob("*.json"))) if st.runs.is_dir() else 0,
                    traces=len(list(st.traces.glob("*.jsonl"))) if st.traces.is_dir() else 0)
        out.append(info)
    out.sort(key=lambda i: i.get("first_seen") or "", reverse=True)
    return out


def _git_remote(root: Path) -> str:
    import subprocess
    try:
        r = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=root, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""
