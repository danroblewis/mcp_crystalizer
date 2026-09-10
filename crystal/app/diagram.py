"""Research-flow diagram: an inline SVG DAG of the steps, laid out in Python (no JS layout library).

Nodes are steps (colour by information type, badge by hits, fan-outs drawn as a stack with an item count and a hit
summary); edges are data flow. For a crystallized flow the edges come from the YAML templates: every `{{ ... }}` /
`{% ... %}` in a step's args, plus its `forEach` and `when` expressions, is scanned for `stepid.` and `inputs.`
references. For a recorded agent trace the caller passes the edges (values carried forward between calls).
"""
from __future__ import annotations

import html
import re
from collections import defaultdict
from typing import Any

# information types in the dataviz categorical order (fixed, never cycled); owners/people take the neutral slot
TYPE_COLORS: dict[str, str] = {
    "issue": "#2a78d6", "message": "#eb6834", "log": "#1baf7a", "metric": "#eda100", "page": "#e87ba4",
    "code": "#008300", "commit": "#4a3aa7", "incident": "#e34948", "owners": "#6b7280", "input": "#1b211e",
    "flow": "#6b7280", "other": "#6b7280",
}
TYPE_LABELS: dict[str, str] = {
    "issue": "ticket", "message": "messages", "log": "log lines", "metric": "metric", "page": "pages", "code": "code",
    "commit": "commits", "incident": "incident", "owners": "owners", "input": "inputs", "flow": "flow", "other": "other",
}

_TEMPLATE = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)
_IDENT = re.compile(r"(?<![\w.\"'])([A-Za-z_]\w*)\s*\.")


def _scan_expr(expr: str, known: set[str]) -> set[str]:
    return {name for name in _IDENT.findall(expr or "") if name in known or name == "inputs"}


def template_refs(value: Any, known: set[str]) -> set[str]:
    """Step ids (and 'inputs') referenced by the Jinja templates inside a string / list / dict of args."""
    found: set[str] = set()
    if isinstance(value, str):
        for m in _TEMPLATE.finditer(value):
            found |= _scan_expr(m.group(1) or m.group(2) or "", known)
    elif isinstance(value, list):
        for v in value:
            found |= template_refs(v, known)
    elif isinstance(value, dict):
        for v in value.values():
            found |= template_refs(v, known)
    return found


def flow_edges(flow: dict) -> list[tuple[str, str]]:
    """(source, target) pairs derived from the flow YAML: which step ids each step's args/forEach/when reference."""
    steps = flow.get("steps") or []
    known = {s["id"] for s in steps}
    edges: list[tuple[str, str]] = []
    for s in steps:
        refs = template_refs(s.get("args"), known)
        if s.get("forEach"):
            refs |= _scan_expr(str(s["forEach"]), known)
        if s.get("when") is not None:
            refs |= template_refs(str(s["when"]), known) | _scan_expr(str(s["when"]), known)
        for r in sorted(refs):
            if r != s["id"]:
                edges.append((r, s["id"]))
    return edges


# ---------------------------------------------------------------- layout

NODE_W, NODE_H, H_GAP, V_GAP, PAD = 164, 40, 64, 16, 12


def layered_layout(nodes: list[dict], edges: list[tuple[str, str]]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Left-to-right layers: a node sits one layer right of its furthest predecessor. Only forward edges (source
    earlier in the node list) count, which keeps the graph acyclic. Rows within a layer follow the barycentre of the
    predecessors' rows, ties by node order. Mutates and returns the nodes (adds layer/row/x/y) and the kept edges."""
    index = {n["id"]: i for i, n in enumerate(nodes)}
    kept = [(a, b) for a, b in edges if a in index and b in index and index[a] < index[b]]
    preds: dict[str, list[str]] = defaultdict(list)
    for a, b in kept:
        preds[b].append(a)
    layer: dict[str, int] = {}
    for n in nodes:
        layer[n["id"]] = max((layer[p] + 1 for p in preds[n["id"]]), default=0)
    by_layer: dict[int, list[dict]] = defaultdict(list)
    for n in nodes:
        n["layer"] = layer[n["id"]]
        by_layer[n["layer"]].append(n)
    row: dict[str, float] = {}
    for L in sorted(by_layer):
        members = by_layer[L]
        if L > 0:
            members.sort(key=lambda n: (sum(row[p] for p in preds[n["id"]]) / len(preds[n["id"]]) if preds[n["id"]] else 1e9, index[n["id"]]))
        for r, n in enumerate(members):
            n["row"] = r
            row[n["id"]] = r
            n["x"] = PAD + L * (NODE_W + H_GAP)
            n["y"] = PAD + r * (NODE_H + V_GAP)
    return nodes, kept


def _trunc(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def svg(nodes: list[dict], edges: list[tuple[str, str]], href: str = "#s-{id}") -> str:
    """Render the laid-out graph. Node dict: id, title, type, hits, items (fan-out size, optional), item_hits,
    skipped, error. Each node is wrapped in <a href> so clicking jumps to that step's evidence."""
    nodes, edges = layered_layout([dict(n) for n in nodes], edges)
    pos = {n["id"]: n for n in nodes}
    width = max((n["x"] for n in nodes), default=0) + NODE_W + PAD + 8
    height = max((n["y"] for n in nodes), default=0) + NODE_H + PAD + 8
    out = [f'<svg class="dag" xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="research flow: {len(nodes)} steps">',
           '<defs><marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#9aa39e"/></marker></defs>']
    for a, b in edges:
        s, t = pos[a], pos[b]
        x1, y1 = s["x"] + NODE_W, s["y"] + NODE_H / 2
        x2, y2 = t["x"], t["y"] + NODE_H / 2
        dx = max(24, (x2 - x1) / 2)
        out.append(f'<path class="edge" d="M{x1:.0f},{y1:.0f} C{x1 + dx:.0f},{y1:.0f} {x2 - dx:.0f},{y2:.0f} {x2:.0f},{y2:.0f}" fill="none" stroke="#b9c0bc" stroke-width="1.3" marker-end="url(#arr)"/>')
    for n in nodes:
        color = TYPE_COLORS.get(n.get("type") or "other", TYPE_COLORS["other"])
        hits = n.get("hits")
        skipped, error = n.get("skipped"), n.get("error")
        dead = skipped or (not hits and not n.get("items"))
        stroke_w = 2.2 if hits else 1.2
        dash = ' stroke-dasharray="4 3"' if dead else ""
        fill = "#ffffff" if dead else _tint(color)
        title = html.escape(_trunc(n.get("title") or n["id"], 24))
        if skipped:
            sub = "skipped"
        elif n.get("items") is not None:
            k = n["items"]
            sub = f"{k} item{'s' if k != 1 else ''}, {n.get('item_hits', 0)} hit, {_plural(hits or 0, 'result')}"
        elif error:
            sub = "error"
        else:
            sub = _plural(hits or 0, "result") if hits is not None else n.get("sub", "")
        tip = html.escape(f"{n.get('title') or n['id']} [{TYPE_LABELS.get(n.get('type') or 'other', 'other')}] {sub}")
        link = n.get("href") or href.format(id=n["id"])
        g = [f'<a href="{html.escape(link)}" class="node-link" data-step="{html.escape(n["id"])}">',
             f'<g class="node t-{html.escape(n.get("type") or "other")}" transform="translate({n["x"]},{n["y"]})"><title>{tip}</title>']
        if n.get("items") is not None:
            for off in (8, 4):
                g.append(f'<rect x="{off}" y="{off}" width="{NODE_W}" height="{NODE_H}" rx="5" fill="#fff" stroke="{color}" stroke-width="1" opacity="0.7"/>')
        g.append(f'<rect width="{NODE_W}" height="{NODE_H}" rx="5" fill="{fill}" stroke="{color}" stroke-width="{stroke_w}"{dash}/>')
        g.append(f'<rect x="0" y="0" width="4" height="{NODE_H}" rx="2" fill="{color}"/>')
        g.append(f'<text x="11" y="17" font-size="11.5" font-weight="600" fill="#1b211e">{title}</text>')
        g.append(f'<text x="11" y="31" font-size="10.5" fill="#5c6662">{html.escape(sub)}</text>')
        if hits:
            badge = str(hits)
            bw = 10 + 6.5 * len(badge)
            g.append(f'<rect x="{NODE_W - bw - 5}" y="5" width="{bw:.0f}" height="15" rx="7.5" fill="{color}"/>')
            g.append(f'<text x="{NODE_W - bw / 2 - 5:.0f}" y="16" font-size="10" font-weight="600" fill="#fff" text-anchor="middle">{badge}</text>')
        g.append("</g></a>")
        out.append("".join(g))
    out.append("</svg>")
    return "\n".join(out)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tint(hex_color: str, alpha: float = 0.10) -> str:
    """A light tint of a hex colour over white (so fills stay readable under the label text)."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: int(round(255 + (c - 255) * alpha))  # noqa: E731
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"
