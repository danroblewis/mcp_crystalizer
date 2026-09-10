"""Promotion lifecycle and circuit breaker for flows. State lives in state/lifecycle.sqlite (gitignored).

Two notions of status:
  * author intent   - the `status` field in the flow YAML (draft | candidate | promoted). Authoritative; edited by hand.
  * effective state - what the runtime currently trusts, kept here. Starts equal to author intent, moves down one
    level on each failure signal (circuit breaker) and back up after N consecutive clean live runs. It never rises
    above author intent: promotion beyond that is the author's call (edit the YAML).

Failure signals: a run with a step error, a required step with zero hits, a failed run status, a failed regression
test (`crystal test`), or a user's "this didn't help" from the UI. Only live runs (kind="run") count toward
re-promotion: a passing regression test replays the same recorded responses every time, so it is recorded
(tests_passed, last_test) but never advances the clean streak.

Per-flow knobs in the YAML (all optional):
  promote_after: 5      clean runs needed for candidate -> promoted (default 5)
  candidate_after: 2    clean runs needed for draft -> candidate (default 2)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crystal import PROJECT_ROOT

STATE_DIR = PROJECT_ROOT / "state"
DEFAULT_DB = STATE_DIR / "lifecycle.sqlite"
LEVELS = ["draft", "candidate", "promoted"]
DEFAULT_PROMOTE_AFTER = 5
DEFAULT_CANDIDATE_AFTER = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
  name TEXT PRIMARY KEY,
  author_status TEXT NOT NULL,
  status TEXT NOT NULL,
  clean_streak INTEGER NOT NULL DEFAULT 0,
  clean_runs INTEGER NOT NULL DEFAULT 0,
  failed_runs INTEGER NOT NULL DEFAULT 0,
  total_runs INTEGER NOT NULL DEFAULT 0,
  tests_passed INTEGER NOT NULL DEFAULT 0,
  tests_failed INTEGER NOT NULL DEFAULT 0,
  complaints INTEGER NOT NULL DEFAULT 0,
  last_failure TEXT,
  last_failure_at TEXT,
  last_failure_run TEXT,
  last_run_at TEXT,
  last_test TEXT,
  promote_after INTEGER NOT NULL DEFAULT 5,
  candidate_after INTEGER NOT NULL DEFAULT 2,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  flow TEXT NOT NULL,
  kind TEXT NOT NULL,          -- run | test | feedback | transition | sync
  ok INTEGER,
  detail TEXT,
  run_id TEXT,
  from_status TEXT,
  to_status TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def level(status: str | None) -> int:
    return LEVELS.index(status) if status in LEVELS else 0


def classify_run(record: dict, flow: dict | None = None) -> str | None:
    """Return the failure reason for a run record, or None if the run was clean.

    Failure = run status not ok, any step (or fan-out item) with an error, or a required step with zero hits.
    """
    if record.get("status") not in (None, "ok"):
        return f"run status {record.get('status')}"
    required = {s["id"] for s in (flow or {}).get("steps", []) if s.get("required")}
    for s in record.get("steps", []):
        if s.get("skipped"):
            continue
        if s.get("error"):
            return f"step {s['id']}: {str(s['error'])[:200]}"
        if s["id"] in required and not (s.get("hits") or 0):
            return f"required step {s['id']} returned zero hits"
    return None


class Lifecycle:
    def __init__(self, path: Path | str | None = None):
        p = Path(path or os.environ.get("CRYSTAL_LIFECYCLE_DB") or DEFAULT_DB)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = p
        # one connection per store, shared across threads (an MCP server runs sync tools in a worker thread and async
        # ones on its loop); writes are short and serialised by the lock in record_outcome
        self.db = sqlite3.connect(str(p), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- reading
    def get(self, name: str) -> dict | None:
        row = self.db.execute("SELECT * FROM flows WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def all(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM flows ORDER BY name")]

    def events(self, name: str | None = None, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM events", ()
        if name:
            q, args = q + " WHERE flow = ?", (name,)
        q += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(q, (*args, limit))]

    def view(self, flow: dict) -> dict:
        """Effective state for a flow dict, synced with its author status. Safe to call from the UI."""
        return self.sync(flow)

    # ---------------------------------------------------------------- sync with author intent
    def sync(self, flow: dict) -> dict:
        """Make sure a row exists and reflects the author's status. A changed author status is a new declaration:
        the effective state is reset to it and the streak cleared (counters are kept for history)."""
        with self._lock:
            return self._sync(flow)

    def _sync(self, flow: dict) -> dict:
        name = flow["name"]
        author = flow.get("status") if flow.get("status") in LEVELS else "draft"
        pa = int(flow.get("promote_after") or DEFAULT_PROMOTE_AFTER)
        ca = int(flow.get("candidate_after") or DEFAULT_CANDIDATE_AFTER)
        row = self.get(name)
        if row is None:
            self.db.execute("INSERT INTO flows (name, author_status, status, promote_after, candidate_after, updated_at) VALUES (?,?,?,?,?,?)",
                            (name, author, author, pa, ca, _now()))
            self._event(name, "sync", None, f"first seen; author status {author}", None, None, author)
            self.db.commit()
            return self.get(name)
        if row["author_status"] != author:
            self.db.execute("UPDATE flows SET author_status=?, status=?, clean_streak=0, promote_after=?, candidate_after=?, updated_at=? WHERE name=?",
                            (author, author, pa, ca, _now(), name))
            self._event(name, "sync", None, f"author status changed {row['author_status']} -> {author}", None, row["status"], author)
            self.db.commit()
        elif (row["promote_after"], row["candidate_after"]) != (pa, ca):
            self.db.execute("UPDATE flows SET promote_after=?, candidate_after=?, updated_at=? WHERE name=?", (pa, ca, _now(), name))
            self.db.commit()
        return self.get(name)

    # ---------------------------------------------------------------- signals
    def record_run(self, record: dict, flow: dict | None = None) -> dict:
        """Count a live run. Returns the resulting state plus `outcome` and `reason`."""
        flow = flow or {"name": record["flow"], "status": "draft"}
        reason = classify_run(record, flow)
        return self.record_outcome(flow, ok=reason is None, reason=reason, kind="run", run_id=record.get("run_id"))

    def record_test(self, flow: dict, passed: bool, detail: str = "") -> dict:
        with self._lock:
            state = self.record_outcome(flow, ok=passed, reason=None if passed else (detail or "regression test failed"), kind="test")
            self.db.execute("UPDATE flows SET tests_passed = tests_passed + ?, tests_failed = tests_failed + ?, last_test = ?, updated_at = ? WHERE name = ?",
                            (1 if passed else 0, 0 if passed else 1, json.dumps({"ts": _now(), "passed": passed, "detail": detail[:500]}), _now(), flow["name"]))
            self.db.commit()
            return self.get(flow["name"]) | {"outcome": state["outcome"], "reason": state["reason"], "transition": state.get("transition")}

    def record_feedback(self, flow: dict, run_id: str | None, text: str = "") -> dict:
        with self._lock:
            self.sync(flow)
            self.db.execute("UPDATE flows SET complaints = complaints + 1 WHERE name = ?", (flow["name"],))
            return self.record_outcome(flow, ok=False, reason=f"user: {text.strip() or 'this did not help'}", kind="feedback", run_id=run_id)

    def record_outcome(self, flow: dict, ok: bool, reason: str | None, kind: str = "run", run_id: str | None = None) -> dict:
        """The state machine. Clean live run: streak += 1, promote one level when the streak reaches the threshold for
        the next level (never above author intent). A clean signal of any other kind (a passing test) is logged only.
        Failure of any kind: streak = 0, demote one level (never below draft)."""
        with self._lock:
            return self._record_outcome(flow, ok, reason, kind, run_id)

    def _record_outcome(self, flow: dict, ok: bool, reason: str | None, kind: str, run_id: str | None) -> dict:
        row = self.sync(flow)
        name, cur, author = row["name"], row["status"], row["author_status"]
        now = _now()
        self._event(name, kind, ok, reason, run_id, None, None)
        if kind == "run":
            self.db.execute("UPDATE flows SET total_runs = total_runs + 1, last_run_at = ? WHERE name = ?", (now, name))
        new = cur
        if ok and kind != "run":
            pass                                   # a passing test is not live evidence: no streak, no transition
        elif ok:
            streak = row["clean_streak"] + 1
            self.db.execute("UPDATE flows SET clean_streak = ?, clean_runs = clean_runs + 1, updated_at = ? WHERE name = ?", (streak, now, name))
            if level(cur) < level(author):
                need = row["candidate_after"] if cur == "draft" else row["promote_after"]
                if streak >= need:
                    new = LEVELS[level(cur) + 1]
                    self.db.execute("UPDATE flows SET status = ?, clean_streak = 0, updated_at = ? WHERE name = ?", (new, now, name))
                    self._event(name, "transition", True, f"{streak} clean live runs", run_id, cur, new)
        else:
            self.db.execute("UPDATE flows SET clean_streak = 0, failed_runs = failed_runs + ?, last_failure = ?, last_failure_at = ?, last_failure_run = ?, updated_at = ? WHERE name = ?",
                            (1 if kind == "run" else 0, (reason or "failure")[:500], now, run_id, now, name))
            if level(cur) > 0:
                new = LEVELS[level(cur) - 1]
                self.db.execute("UPDATE flows SET status = ?, updated_at = ? WHERE name = ?", (new, now, name))
                self._event(name, "transition", False, f"circuit breaker ({kind}): {reason}", run_id, cur, new)
        self.db.commit()
        return self.get(name) | {"outcome": "clean" if ok else "failure", "reason": reason, "transition": (cur, new) if new != cur else None}

    def _event(self, flow, kind, ok, detail, run_id, from_status, to_status) -> None:
        self.db.execute("INSERT INTO events (ts, flow, kind, ok, detail, run_id, from_status, to_status) VALUES (?,?,?,?,?,?,?,?)",
                        (_now(), flow, kind, None if ok is None else int(bool(ok)), detail, run_id, from_status, to_status))


def describe(state: dict | None) -> dict[str, Any]:
    """Small dict for templates/CLI: badge, tripped flag, next-promotion hint."""
    if not state:
        return {"status": "?", "tripped": False, "hint": ""}
    tripped = level(state["status"]) < level(state["author_status"])
    hint = ""
    if tripped:
        need = state["candidate_after"] if state["status"] == "draft" else state["promote_after"]
        hint = f"{state['clean_streak']}/{need} clean live runs to re-promote to {LEVELS[level(state['status']) + 1]}"
    return {"status": state["status"], "tripped": tripped, "hint": hint}


_default: Lifecycle | None = None


def get_lifecycle() -> Lifecycle:
    """Process-wide default store (honours CRYSTAL_LIFECYCLE_DB)."""
    global _default
    if _default is None or str(_default.path) != str(Path(os.environ.get("CRYSTAL_LIFECYCLE_DB") or DEFAULT_DB)):
        _default = Lifecycle()
    return _default


def status_table(flows: list[dict], lc: Lifecycle | None = None) -> list[dict]:
    lc = lc or get_lifecycle()
    out = []
    for f in flows:
        if f.get("status") == "broken":
            continue
        st = lc.sync(f)
        out.append({**st, **describe(st), "author_status": st["author_status"], "title": f.get("title", "")})
    return out
