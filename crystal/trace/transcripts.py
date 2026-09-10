"""Import past Claude Code sessions as traces, for free.

Claude Code keeps every session transcript under ~/.claude/projects/<project-slug>/<session-id>.jsonl: one JSON
object per line, {type: user|assistant, cwd, sessionId, timestamp, message: {role, content: [blocks]}} plus summary
and bookkeeping lines (mode, attachment, file-history-*, ...) that are skipped. An assistant `tool_use` block
({id, name, input}) is answered by a `tool_result` block ({tool_use_id, content, is_error}) on a following user line;
the content is a string or a list of {type: text, text} blocks. A plain user text block is a prompt.

Subagents (the Agent tool) run as sidechains: their lines carry `isSidechain: true` and an `agentId`, either inside
the session file (older versions) or in `<session-id>/subagents/agent-<id>.jsonl` next to it, with an
`agent-<id>.meta.json` naming the parent's Agent `toolUseId`. Every call is tagged with its `agent` ("main" or the
agent id) and with the prompt it ran under; a subagent's calls belong to the main prompt whose Agent call spawned it.

`parse_transcript()` turns a session into calls paired by tool_use id, with outputs parsed exactly the way the hook
parses them (JSON in the text is decoded; Claude Code's own Read/Grep/Glob/Bash keep a short preview).
`import_transcripts()` maps each transcript's `cwd` to its workspace (crystal.state) and writes the same record shape
the hook writes into that workspace's trace dir: source "transcript", trigger "prompt", session id = Claude's session
id. Only transcripts with at least one MCP call are imported, re-imports skip sessions already present unless
`force`, and a session with more than one (prompt, agent) pair that made MCP calls is also split into episodes:
`<sessionId>-e<N>` for the main thread's calls under prompt N and `<sessionId>-e<N>-a<agentId>` for each subagent
spawned under it. Those episodes are what induction and mining work on.

Reattribution: the PostToolUse hook fires for a subagent's calls under the parent session id, so a hook-recorded
trace of a session with parallel subagents interleaves their calls with no attribution. When the hook trace of a
session exists, importing its transcript joins the hook records to the transcript on `tool_use_id`, adds `agent`
and `prompt_index` to each call record in place, and writes the per-agent episode files from the hook records
(their outputs are the precious ones). Nothing under ~/.claude is ever written or moved: transcripts are only read.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from crystal import state as state_mod
from crystal.extract import ids
from crystal.trace.builtin_map import map_call
from crystal.trace.record import CLAUDE_CODE_TOOLS, Recorder, _preview, split_tool_name

TRANSCRIPTS_ENV = "MCP_EXPLORER_TRANSCRIPTS"
DEFAULT_TRANSCRIPTS = "~/.claude/projects"
SOURCE = "transcript"
HOOK_SOURCE = "claude-code"
TRIGGER = "prompt"
MAIN = "main"
PROMPT_LIMIT = 4000            # meta.prompt / inputs.prompt keep this much; the prompt record keeps everything
INJECTED_PREFIXES = ("<task-notification>", "<local-command", "<command-name>", "<command-message>", "<system-reminder>",
                     "<bash-input>", "<bash-stdout>", "<bash-stderr>", "<user-memory-input>", "<ide_", "[Request interrupted")
AGENT_TOOLS = ("Agent", "Task")


def transcripts_dir(override: str | os.PathLike | None = None) -> Path:
    """`--transcripts <dir>` > $MCP_EXPLORER_TRANSCRIPTS > ~/.claude/projects."""
    raw = str(override) if override not in (None, "") else os.environ.get(TRANSCRIPTS_ENV, "").strip()
    return Path(raw or DEFAULT_TRANSCRIPTS).expanduser()


def find_transcripts(base: Path | None = None) -> list[Path]:
    """Every `<project>/<session>.jsonl` under the base (and bare `<session>.jsonl` files, so a flat directory of
    transcripts works too). Subagent transcripts live under `<session>/subagents/` and are read with their parent."""
    base = base if base is not None else transcripts_dir()
    if not base.is_dir():
        return []
    out = sorted(base.glob("*.jsonl")) + sorted(base.glob("*/*.jsonl"))
    return [p for p in out if p.is_file()]


def subagent_files(path: Path) -> list[Path]:
    d = path.parent / path.stem / "subagents"
    return sorted(d.glob("agent-*.jsonl")) if d.is_dir() else []


@dataclass
class Call:
    seq: int
    server: str
    tool: str
    input: dict
    output: Any
    output_text: str
    is_error: bool
    ts: str | None
    tool_use_id: str
    prompt_index: int          # index into Transcript.prompts of the main prompt this call ran under (-1: before any)
    prompt: str | None         # that prompt's text
    agent: str = MAIN          # "main" or the subagent id

    @property
    def is_mcp(self) -> bool:
        return self.server != "claude-code"


@dataclass
class Transcript:
    path: Path
    session_id: str
    cwd: str | None
    prompts: list[dict] = field(default_factory=list)      # {index, text, ts}
    calls: list[Call] = field(default_factory=list)        # MCP calls, plus Read/Grep/Glob/Bash once the session is active; by time
    result: str | None = None                              # the last assistant text of the main thread
    results_by_prompt: dict[int, str] = field(default_factory=dict)   # last main-thread text under each prompt
    agents: dict[str, dict] = field(default_factory=dict)  # agent id -> {type, description, tool_use_id, prompt, prompt_index, result, path}
    lines: int = 0
    skipped_tools: int = 0                                 # tool_use blocks that are neither MCP nor a recorded built-in

    @property
    def mcp_calls(self) -> list[Call]:
        return [c for c in self.calls if c.is_mcp]

    @property
    def servers(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.mcp_calls:
            out[c.server] = out.get(c.server, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    @property
    def first_prompt(self) -> str | None:
        return self.prompts[0]["text"] if self.prompts else None

    def episode_prompts(self) -> list[int]:
        """The main prompts that have at least one MCP call under them (by any agent)."""
        return sorted({c.prompt_index for c in self.mcp_calls if c.prompt_index >= 0})

    def episode_keys(self) -> list[tuple[int, str]]:
        """(prompt index, agent) pairs with at least one MCP call: the episodes. Main thread first within a prompt."""
        keys = {(c.prompt_index, c.agent) for c in self.mcp_calls if c.prompt_index >= 0}
        return sorted(keys, key=lambda k: (k[0], k[1] != MAIN, k[1]))

    def episode_count(self) -> int:
        """How many episode files an import writes: none unless the session splits into more than one."""
        n = len(self.episode_keys())
        return n if n > 1 else 0

    def episode_numbers(self) -> dict[tuple[int, str], str]:
        """Episode key -> suffix: `e<N>` for the main thread under the N-th prompt with MCP calls, `e<N>-a<id>` for a
        subagent spawned under it."""
        order = {pi: n for n, pi in enumerate(self.episode_prompts(), 1)}
        return {(pi, agent): f"e{order[pi]}" + ("" if agent == MAIN else f"-a{agent}") for pi, agent in self.episode_keys()}


def _iter_lines(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def _is_injected(text: str) -> bool:
    t = text.lstrip()
    return any(t.startswith(p) for p in INJECTED_PREFIXES)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_result(content: Any) -> tuple[Any, str]:
    """The (output, output_text) pair the hook records: text blocks joined, JSON in the text decoded."""
    output_text = _text_of(content)
    output: Any = content
    if output_text:
        try:
            output = json.loads(output_text)
        except json.JSONDecodeError:
            output = output_text
    return output, output_text


class _Thread:
    """Parser state of one thread of a session: the main conversation or one subagent (sidechain)."""

    def __init__(self, agent: str):
        self.agent = agent
        self.pending: dict[str, dict] = {}       # tool_use id -> {id, name, input, ts, prompt_index}
        self.prompt_index = -1                   # main: index of the current prompt; subagent: the spawning prompt's
        self.prompts: list[dict] = []            # main thread only
        self.last_text: str | None = None
        self.results_by_prompt: dict[int, str] = {}
        self.first_prompt: str | None = None     # subagent: the instruction it was given
        self.first_ts: str | None = None
        self.calls: list[Call] = []
        self.agent_uses: dict[str, dict] = {}    # Agent tool_use id -> {prompt_index, ts, description, prompt}


class _Parser:
    def __init__(self, tx: Transcript):
        self.tx = tx
        self.active = False                      # session-level, as the hook: Read/Grep/Glob/Bash count once an MCP call was made
        self.threads: dict[str, _Thread] = {}

    def thread(self, rec: dict, default: str) -> _Thread:
        agent = str(rec.get("agentId") or default) if rec.get("isSidechain") else default
        if agent not in self.threads:
            self.threads[agent] = _Thread(agent)
        return self.threads[agent]

    def feed(self, path: Path, default_agent: str) -> None:
        tx = self.tx
        for rec in _iter_lines(path):
            tx.lines += 1
            typ = rec.get("type")
            if typ not in ("user", "assistant"):
                continue
            if not tx.cwd and rec.get("cwd"):
                tx.cwd = str(rec["cwd"])
            if rec.get("sessionId") and tx.session_id == tx.path.stem and default_agent == MAIN:
                tx.session_id = str(rec["sessionId"])
            th = self.thread(rec, default_agent)
            th.first_ts = th.first_ts or rec.get("timestamp")
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            ts = rec.get("timestamp")
            if typ == "user":
                self.user(th, rec, content, ts)
            else:
                self.assistant(th, content, ts)

    def user(self, th: _Thread, rec: dict, content: Any, ts: str | None) -> None:
        blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}] if isinstance(content, str) else [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                use = th.pending.pop(str(b.get("tool_use_id")), None)
                if use is None:
                    continue
                server, tool = split_tool_name(use["name"])
                args = use["input"]
                if use.get("mapped"):
                    server, tool, args = use["mapped"]      # recorded as the built-in server that can replay it
                is_err = bool(b.get("is_error"))
                output, output_text = parse_result(b.get("content"))
                if server == "claude-code":
                    output, output_text = _preview(output), _preview(output_text)
                th.calls.append(Call(seq=0, server=server, tool=tool, input=args, output=output, output_text=output_text,
                                     is_error=is_err, ts=use["ts"] or ts, tool_use_id=use["id"], prompt_index=use["prompt_index"],
                                     prompt=None, agent=th.agent))
            elif b.get("type") == "text" and not rec.get("isMeta"):
                text = str(b.get("text") or "")
                if not text.strip() or _is_injected(text):
                    continue
                if th.agent != MAIN:
                    th.first_prompt = th.first_prompt or text      # the subagent's instruction; later texts are tool chatter
                    continue
                if th.last_text and th.prompt_index >= 0:
                    th.results_by_prompt[th.prompt_index] = th.last_text
                th.prompt_index = len(th.prompts)
                th.prompts.append({"index": th.prompt_index, "text": text, "ts": ts})
                th.last_text = None

    def assistant(self, th: _Thread, content: Any, ts: str | None) -> None:
        if isinstance(content, str):
            if content.strip():
                th.last_text = content.strip()
            return
        if not isinstance(content, list):
            return
        texts = [str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t).strip()
        if joined:
            th.last_text = joined
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = str(b.get("name") or "")
            inp = b.get("input") or {}
            if name in AGENT_TOOLS and th.agent == MAIN:
                th.agent_uses[str(b.get("id"))] = {"prompt_index": th.prompt_index, "ts": ts, "description": inp.get("description"),
                                                   "prompt": inp.get("prompt")}
            server, tool = split_tool_name(name)
            mapped = None
            if server == "claude-code":
                # Most sessions never touch an MCP server; their Read/Grep/Glob/git calls are the investigation, and
                # the built-in code/git servers can replay them, so record those under the server that can.
                mapped = map_call(tool, inp)
                if mapped is None and (tool not in CLAUDE_CODE_TOOLS or not self.active):
                    self.tx.skipped_tools += 1
                    continue
            else:
                self.active = True
            th.pending[str(b.get("id"))] = {"id": str(b.get("id")), "name": name, "input": inp, "ts": ts,
                                            "prompt_index": th.prompt_index, "mapped": mapped}


def _agent_meta(path: Path) -> dict:
    meta = path.with_suffix("").with_suffix(".meta.json")   # agent-<id>.jsonl -> agent-<id>.meta.json
    if meta.is_file():
        try:
            return json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def parse_transcript(path: Path) -> Transcript:
    """Read one session: the transcript, plus its subagent transcripts (`<stem>/subagents/agent-*.jsonl`) and any
    sidechain lines inside it. Bookkeeping lines are skipped; meta user lines (image placeholders, local-command
    caveats) and injected text (task notifications, command output) never count as prompts, so the calls they
    carry belong to the prompt before them. Claude Code's own Read/Grep/Glob/Bash are kept only once the session
    has made an MCP call, the same rule the hook applies; every other built-in tool (Edit, Write, Agent, ...) is
    dropped. Calls come back ordered by time, each tagged with its agent and the main prompt it ran under."""
    tx = Transcript(path=path, session_id=path.stem, cwd=None)
    parser = _Parser(tx)
    parser.feed(path, MAIN)
    sub_paths: dict[str, Path] = {}
    for sp in subagent_files(path):
        agent = sp.stem[len("agent-"):]
        sub_paths[agent] = sp
        parser.feed(sp, agent)
    main = parser.threads.get(MAIN) or _Thread(MAIN)
    tx.prompts = main.prompts
    tx.results_by_prompt = dict(main.results_by_prompt)
    if main.last_text:
        tx.result = main.last_text
        if main.prompt_index >= 0:
            tx.results_by_prompt.setdefault(main.prompt_index, main.last_text)
    # subagents: which main prompt spawned each (the Agent tool_use named by its meta.json, else the latest prompt
    # before its first line), its instruction and its last message
    calls: list[Call] = list(main.calls)
    for agent, th in parser.threads.items():
        if agent == MAIN:
            continue
        meta = _agent_meta(sub_paths[agent]) if agent in sub_paths else {}
        use = main.agent_uses.get(str(meta.get("toolUseId") or ""))
        if use is None:
            use = next((u for u in main.agent_uses.values() if u.get("prompt") and th.first_prompt and u["prompt"] == th.first_prompt), None)
        if use is not None:
            pi = use["prompt_index"]
        else:
            before = [p for p in main.prompts if (p.get("ts") or "") <= (th.first_ts or "")]
            pi = before[-1]["index"] if before else (main.prompts[0]["index"] if main.prompts else -1)
        tx.agents[agent] = {"type": meta.get("agentType"), "description": meta.get("description") or (use or {}).get("description"),
                            "tool_use_id": meta.get("toolUseId"), "prompt": th.first_prompt or (use or {}).get("prompt"),
                            "prompt_index": pi, "result": th.last_text, "path": str(sub_paths[agent]) if agent in sub_paths else None}
        for c in th.calls:
            c.prompt_index = pi
        calls.extend(th.calls)
    calls.sort(key=lambda c: (c.ts or "", c.agent != MAIN))
    for i, c in enumerate(calls, 1):
        c.seq = i
        c.prompt = tx.prompts[c.prompt_index]["text"] if 0 <= c.prompt_index < len(tx.prompts) else None
    tx.calls = calls
    return tx


# ---------------------------------------------------------------- inputs from a prompt
# Types that identify an entity a flow could be parameterised by. Deliberately excludes the shapes that match
# almost any text (http_status is any 3-digit number; dates, emails, urls and ips litter fetched page content),
# which otherwise become inputs the UI demands and nothing uses.
INPUT_TYPES = frozenset({"jira_key", "sha40", "sha_short", "trace_id", "trace_id16", "uuid", "k8s_pod",
                         "slack_ts", "slack_channel_id", "slack_channel", "slack_user_id", "slack_dm_id",
                         "pd_incident_id", "pd_service_id"})


def prompt_inputs(text: str | None, calls: list[dict] | None = None) -> dict:
    """What a flow induced from this session would take: the prompt, plus the identifiers it mentions that the
    session actually passed to a tool (a jira key, a channel, a sha). A value the prompt merely contains -- a
    session id inside a path, an address quoted from a page -- is not an input: nothing would consume it."""
    if not text:
        return {}
    out: dict[str, Any] = {"prompt": text[:PROMPT_LIMIT]}
    if calls is None:
        return out
    used = json.dumps([(c.get("input") if isinstance(c, dict) else getattr(c, "input", None)) or {} for c in calls], default=str)
    for m in ids.typed_mentions(text):
        if m["type"] in INPUT_TYPES and m["value"] in used:
            out.setdefault(m["type"], m["value"])
    return out


# ---------------------------------------------------------------- writing traces
def _episode_meta(tx: Transcript, pi: int, agent: str, n: str) -> tuple[dict, dict | None, str | None]:
    """(meta extras, prompt record, result) of the episode (prompt pi, agent)."""
    main_prompt = tx.prompts[pi] if 0 <= pi < len(tx.prompts) else None
    extra = {"parent_session": tx.session_id, "episode": n, "prompt_index": pi, "agent": agent}
    if agent == MAIN:
        return extra, main_prompt, tx.results_by_prompt.get(pi)
    info = tx.agents.get(agent, {})
    extra.update(agent_type=info.get("type"), agent_description=info.get("description"),
                 parent_prompt=(main_prompt or {}).get("text", "")[:PROMPT_LIMIT] or None)
    prompt = {"index": pi, "text": info["prompt"], "ts": None} if info.get("prompt") else main_prompt
    return extra, prompt, info.get("result")


def write_session(tx: Transcript, trace_dir: Path, workspace_meta: dict | None = None) -> Path:
    """The whole session as one trace file, in the hook's record format."""
    return _write(tx, trace_dir, tx.session_id, tx.calls, tx.prompts, tx.result, workspace_meta,
                  extra_meta={"episodes": tx.episode_count(), "agents": _agents_meta(tx)})


def _agents_meta(tx: Transcript) -> dict:
    return {a: {k: v for k, v in info.items() if k in ("type", "description", "prompt_index")} for a, info in tx.agents.items()}


def _write(tx: Transcript, trace_dir: Path, session_id: str, calls: list[Call], prompts: list[dict], result: str | None,
           workspace_meta: dict | None, extra_meta: dict | None = None) -> Path:
    first = prompts[0]["text"] if prompts else None
    meta = {"trigger": TRIGGER, "inputs": prompt_inputs(first, calls), "prompt": (first or "")[:PROMPT_LIMIT] or None,
            "cwd": tx.cwd, "cwd_exists": bool(tx.cwd and Path(tx.cwd).is_dir()), "transcript_path": str(tx.path),
            "claude_session_id": tx.session_id, "servers": tx.servers, **(extra_meta or {})}
    if workspace_meta:
        meta["workspace_meta"] = workspace_meta
    path = trace_dir / f"{session_id}.jsonl"
    if path.exists():
        path.unlink()
    rec = Recorder(session_id, SOURCE, trace_dir=trace_dir, meta=meta)
    for p in prompts:
        rec.prompt(p["text"], prompt_index=p["index"], at=p.get("ts"))
    for c in calls:
        rec.record(c.server, c.tool, c.input, c.output, c.output_text, is_error=c.is_error,
                   extra={"tool_use_id": c.tool_use_id, "at": c.ts, "prompt_index": c.prompt_index, "agent": c.agent})
    if result:
        rec.result(result)
    return path


def write_episodes(tx: Transcript, trace_dir: Path, workspace_meta: dict | None = None) -> list[Path]:
    """One trace per (prompt, agent) that made MCP calls, when the session has more than one: `<sessionId>-e<N>`
    for the main thread under the N-th such prompt, `<sessionId>-e<N>-a<agentId>` for each subagent spawned under
    it. Each carries its prompt (a subagent: the instruction it was given) as meta.prompt and inputs, the calls made
    under it, and the thread's last message."""
    if not tx.episode_count():
        return []
    out = []
    for (pi, agent), n in tx.episode_numbers().items():
        calls = [c for c in tx.calls if c.prompt_index == pi and c.agent == agent]
        extra, prompt, result = _episode_meta(tx, pi, agent, n)
        out.append(_write(tx, trace_dir, f"{tx.session_id}-{n}", calls, [prompt] if prompt else [], result, workspace_meta, extra_meta=extra))
    return out


def episodes_of(trace_dir: Path, session_id: str) -> list[Path]:
    return sorted(p for p in trace_dir.glob(f"{session_id}-e*.jsonl") if p.stem[len(session_id) + 2:].split("-")[0].isdigit())


# ---------------------------------------------------------------- reattributing a hook-recorded trace
def hook_trace_of(trace_dir: Path, session_id: str) -> Path | None:
    """The hook's trace of this session, if the hook recorded it (source claude-code), else None."""
    p = trace_dir / f"{session_id}.jsonl"
    if not p.is_file():
        return None
    try:
        with p.open() as fh:
            first = fh.readline()
        return p if json.loads(first).get("source") == HOOK_SOURCE else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def reattribute(tx: Transcript, trace_path: Path, workspace_meta: dict | None = None) -> dict:
    """Join the hook's call records to the transcript on tool_use_id: every call record gets `agent` and
    `prompt_index`, the meta gets the agents and the episode count, and the per-agent episode files are written
    from the hook records. The file is rewritten in place with nothing removed. Returns {joined, unjoined, episodes}."""
    by_id = {c.tool_use_id: c for c in tx.calls}
    records = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    joined = unjoined = 0
    for r in records:
        if r.get("kind") != "call":
            continue
        c = by_id.get(str(r.get("tool_use_id")))
        if c is None:
            unjoined += 1
            continue
        r["agent"], r["prompt_index"] = c.agent, c.prompt_index
        joined += 1
    n_eps = tx.episode_count()
    for r in records:
        if r.get("kind") == "meta":
            r.update(reattributed=True, agents=_agents_meta(tx), episodes=n_eps, transcript_joined=joined, transcript_unjoined=unjoined)
            break
    tmp = trace_path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in records))
    tmp.replace(trace_path)
    trace_dir = trace_path.parent
    for old in episodes_of(trace_dir, tx.session_id):
        old.unlink()
    written = 0
    if n_eps:
        hook_meta = next((r for r in records if r.get("kind") == "meta"), {})
        for (pi, agent), n in tx.episode_numbers().items():
            calls = [r for r in records if r.get("kind") == "call" and r.get("agent") == agent and r.get("prompt_index") == pi]
            if not calls:
                continue
            extra, prompt, result = _episode_meta(tx, pi, agent, n)
            first = prompt["text"] if prompt else None
            meta = {"trigger": TRIGGER, "inputs": prompt_inputs(first, calls), "prompt": (first or "")[:PROMPT_LIMIT] or None,
                    "cwd": hook_meta.get("cwd") or tx.cwd, "transcript_path": str(tx.path), "claude_session_id": tx.session_id,
                    "from_hook_trace": trace_path.name, **extra}
            if workspace_meta:
                meta["workspace_meta"] = workspace_meta
            sid = f"{tx.session_id}-{n}"
            path = trace_dir / f"{sid}.jsonl"
            rec = Recorder(sid, HOOK_SOURCE, trace_dir=trace_dir, meta=meta)
            if prompt:
                rec.prompt(prompt["text"], prompt_index=pi, at=prompt.get("ts"))
            for i, r in enumerate(calls, 1):
                rec.record(r["server"], r["tool"], r.get("input") or {}, r.get("output"), r.get("output_text") or "", is_error=bool(r.get("is_error")),
                           duration_ms=r.get("duration_ms"), extra={"tool_use_id": r.get("tool_use_id"), "at": r.get("ts"), "prompt_index": pi, "agent": agent})
            if result:
                rec.result(result)
            written += 1
    return {"joined": joined, "unjoined": unjoined, "episodes": written}


# ---------------------------------------------------------------- import
def _same_dir(a: str | None, b: Path) -> bool:
    if not a:
        return False
    try:
        return Path(a).expanduser().resolve() == Path(b).resolve()
    except OSError:
        return False


def _workspace_meta(root: Path) -> dict:
    from crystal.workspace import workspace as ws_of
    try:
        return ws_of(root).meta() if root.is_dir() else {"name": root.name, "root": str(root)}
    except Exception:  # noqa: BLE001
        return {"name": root.name, "root": str(root)}


def import_transcripts(base: Path | None = None, workspace_root: Path | None = None, all_projects: bool = False,
                       dry_run: bool = False, force: bool = False, reattribute_again: bool = False,
                       paths: list[Path] | None = None) -> dict:
    """Scan the transcripts and import those with MCP calls. Default: only transcripts whose cwd is
    `workspace_root`; `all_projects` imports every transcript into the workspace its cwd maps to (created if it
    is new; a cwd that no longer exists still gets a state dir, flagged `cwd_exists: false`). A session the hook
    already recorded is not imported twice: its hook trace is reattributed instead (once, or again with
    `reattribute_again`). Returns a report: {found, with_mcp, imported, reattributed, skipped, episodes, servers,
    rows: [...]} where every row describes one transcript."""
    files = paths if paths is not None else find_transcripts(base)
    rows, servers_total = [], {}
    imported = reattributed = skipped = episodes_total = with_mcp = 0
    for p in files:
        try:
            tx = parse_transcript(p)
        except OSError as e:
            rows.append({"path": str(p), "session_id": p.stem, "status": f"unreadable ({e})", "mcp_calls": 0, "servers": {}, "prompts": 0, "episodes": 0, "agents": 0})
            continue
        row = {"path": str(p), "session_id": tx.session_id, "cwd": tx.cwd, "cwd_exists": bool(tx.cwd and Path(tx.cwd).is_dir()),
               "prompts": len(tx.prompts), "mcp_calls": len(tx.mcp_calls), "calls": len(tx.calls), "servers": tx.servers,
               "episodes": tx.episode_count(), "agents": len(tx.agents), "status": "", "first_prompt": tx.first_prompt or ""}
        rows.append(row)
        if not tx.mcp_calls:
            row["status"] = "no MCP calls"
            continue
        with_mcp += 1
        for s, n in tx.servers.items():
            servers_total[s] = servers_total.get(s, 0) + n
        if not tx.cwd:
            row["status"] = "no cwd"
            continue
        if not all_projects and workspace_root is not None and not _same_dir(tx.cwd, workspace_root):
            row["status"] = "other workspace"
            continue
        root = Path(tx.cwd).expanduser()
        st = state_mod.state_for(root)
        row["workspace"] = st.slug
        hook_trace = hook_trace_of(st.traces, tx.session_id) if st.traces.is_dir() else None
        if hook_trace is not None:
            done = '"reattributed": true' in hook_trace.read_text() and not (reattribute_again or force)
            if done:
                row["status"] = "hook trace (reattributed)"
                skipped += 1
                continue
            if dry_run:
                row["status"] = "would reattribute hook trace"
                reattributed += 1
                episodes_total += row["episodes"]
                continue
            res = reattribute(tx, hook_trace, _workspace_meta(root))
            row["status"] = f"reattributed hook trace ({res['joined']} joined, {res['unjoined']} unjoined)"
            row["episodes"] = res["episodes"]
            reattributed += 1
            episodes_total += res["episodes"]
            continue
        exists = (st.traces / f"{tx.session_id}.jsonl").exists()
        if exists and not force:
            row["status"] = "already imported"
            skipped += 1
            continue
        if dry_run:
            row["status"] = "would import" + (" (force)" if exists else "")
            imported += 1
            episodes_total += row["episodes"]
            continue
        st.ensure()
        wmeta = _workspace_meta(root)
        for old in episodes_of(st.traces, tx.session_id):
            old.unlink()
        write_session(tx, st.traces, wmeta)
        eps = write_episodes(tx, st.traces, wmeta)
        row["status"] = "imported" + (" (force)" if exists else "")
        row["episodes"] = len(eps)
        imported += 1
        episodes_total += len(eps)
    return {"found": len(files), "with_mcp": with_mcp, "imported": imported, "reattributed": reattributed, "skipped": skipped,
            "episodes": episodes_total, "servers": dict(sorted(servers_total.items(), key=lambda kv: (-kv[1], kv[0]))), "rows": rows,
            "dry_run": dry_run, "base": str(base if base is not None else transcripts_dir()),
            "all_projects": all_projects, "workspace_root": str(workspace_root) if workspace_root else None}


def importable(base: Path | None, workspace_root: Path) -> list[dict]:
    """What `/import` lists: the rows of a dry run restricted to this workspace, with their import state."""
    rep = import_transcripts(base, workspace_root=workspace_root, all_projects=False, dry_run=True)
    return [r for r in rep["rows"] if r.get("status") not in ("other workspace", "no cwd")]


def format_table(rep: dict, verbose: bool = False) -> str:
    """The CLI table: one row per transcript with MCP calls (every transcript with `verbose`), then totals."""
    lines = []
    hdr = f"{'session':14} {'prompts':>7} {'mcp':>5} {'agents':>6} {'episodes':>8}  {'status':30} {'servers':30} cwd"
    lines.append(hdr)
    for r in rep["rows"]:
        if not verbose and r.get("status") in ("no MCP calls", "no cwd", "other workspace"):
            continue
        servers = ", ".join(f"{s}={n}" for s, n in (r.get("servers") or {}).items())
        cwd = (r.get("cwd") or "?") + ("" if r.get("cwd_exists", True) else "  (directory missing)")
        lines.append(f"{r['session_id'][:14]:14} {r.get('prompts', 0):>7} {r.get('mcp_calls', 0):>5} {r.get('agents', 0):>6} {r.get('episodes', 0):>8}  "
                     f"{r.get('status', ''):30} {servers[:30]:30} {cwd}")
    servers = ", ".join(f"{s}={n}" for s, n in rep["servers"].items())
    verb = "would import" if rep.get("dry_run") else "imported"
    shown = len(lines) - 1
    if not shown and not rep.get("all_projects"):
        # Nothing here is about this directory: say so plainly instead of leaving an empty table under a global total.
        root = rep.get("workspace_root") or "this directory"
        here = rep.get("workspace_root")
        mine = [r for r in rep["rows"] if here and str(r.get("cwd") or "") == str(here)]
        lines = [f"No Claude Code sessions to import for {root}."]
        if not mine:
            lines.append(f"None of the {rep['found']} transcripts in {rep['base']} were recorded in this directory.")
            lines.append("Claude Code deletes transcripts after `cleanupPeriodDays` (default 30), so older sessions are gone;"
                         " raise it in ~/.claude.json to keep future ones.")
        else:
            lines.append(f"{len(mine)} session(s) ran here but made no MCP tool calls, so there is nothing a flow could replay."
                         " Configure MCP servers for this directory and the next session will import.")
        lines.append("Run `mcp-explorer import --all` to see every project on this machine, or `--verbose` for all rows.")
        return "\n".join(lines)
    lines.append(f"{rep['found']} transcripts found in {rep['base']}; {rep['with_mcp']} with MCP calls; {verb} {rep['imported']}; "
                 f"{rep.get('reattributed', 0)} hook traces reattributed; {rep['skipped']} already present; {rep['episodes']} episodes; "
                 f"calls per server: {servers or '-'}")
    return "\n".join(lines)
