"""Read recorded traces back as sessions."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from crystal.trace.record import TRACE_DIR


@dataclass
class Session:
    session_id: str
    source: str
    meta: dict = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    path: Path | None = None

    @property
    def tool_sequence(self) -> list[str]:
        return [f"{c['server']}.{c['tool']}" for c in self.calls]


def load_session(path: Path) -> Session:
    sess = Session(session_id=path.stem, source="?", path=path)
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        sess.source = rec.get("source", sess.source)
        kind = rec.get("kind", "call")
        if kind == "meta":
            sess.meta = {k: v for k, v in rec.items() if k not in ("ts", "session_id", "source", "kind")}
        elif kind == "note":
            sess.notes.append(rec)
        else:
            sess.calls.append(rec)
    return sess


def load_sessions(trace_dir: Path | None = None, trigger: str | None = None) -> list[Session]:
    d = trace_dir or TRACE_DIR
    out = []
    for p in sorted(d.glob("*.jsonl")):
        s = load_session(p)
        if trigger and s.meta.get("trigger") != trigger:
            continue
        if s.calls:
            out.append(s)
    return out
