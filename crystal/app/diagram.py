"""Research-flow diagram: an inline SVG DAG of the steps, laid out in Python (no JS layout library).

Nodes are steps (colour by information type, badge by hits, fan-outs drawn as a stack with an item count and a hit
summary). An EDGE IS A VALUE MOVING, and it is labelled with what moved -- `trace_id`, `error_class`, `service`,
`window` -- not with how many results came back; the count stays on the node badge where it belongs. Several
values along one edge are listed, capped with a "+N". Clicking an edge label jumps to the consuming step's
evidence, exactly as clicking a node does.

For a crystallized flow the edges come from the YAML templates: every `{{ ... }}` / `{% ... %}` in a step's args,
plus its `forEach` and `when` expressions, is scanned for `stepid.` and `inputs.` references -- `template_refs`
gives the step ids, `template_value_refs` also gives the name of the value that moved. For a recorded agent trace
the caller passes the edges (values carried forward between calls). Either way the caller passes `edge_labels`,
built from `crystal.app.provenance`, which knows the concrete values behind each label.

Layout is measured, not guessed: each label's text width is estimated and the gap between two layers widens to fit
the widest label that has to sit in it, then labels sharing a gap are pushed apart vertically until none overlap.
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


_VALUE_REF = re.compile(r"(?<![\w.\"'])([A-Za-z_]\w*)\.(\w+)")


def _expr_value_refs(expr: str, known: set[str]) -> list[tuple[str, str]]:
    """(step id, value name) pairs named by one expression: `issue.error_sig` -> ('issue', 'error_sig')."""
    out = []
    for root, name in _VALUE_REF.findall(expr or ""):
        if (root in known or root == "inputs") and (root, name) not in out:
            out.append((root, name))
    return out


def _template_value_refs(value: Any, known: set[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        for m in _TEMPLATE.finditer(value):
            for pair in _expr_value_refs(m.group(1) or m.group(2) or "", known):
                if pair not in out:
                    out.append(pair)
    elif isinstance(value, list):
        for v in value:
            out.extend(p for p in _template_value_refs(v, known) if p not in out)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(p for p in _template_value_refs(v, known) if p not in out)
    return out


def template_value_refs(flow: dict) -> list[tuple[str, str, str]]:
    """(source step, target step, value name) for every value the YAML moves between steps.

    `flow_edges` answers "which steps are connected"; this answers "with what" -- the label the diagram draws even
    for an edge whose value the run never produced (a missing extract still shows what was meant to travel)."""
    steps = flow.get("steps") or []
    known = {s["id"] for s in steps}
    out: list[tuple[str, str, str]] = []
    for s in steps:
        refs = list(_template_value_refs(s.get("args"), known))
        if s.get("forEach"):
            refs += [p for p in _expr_value_refs(str(s["forEach"]), known) if p not in refs]
        if s.get("when") is not None:
            for p in _template_value_refs(str(s["when"]), known) + _expr_value_refs(str(s["when"]), known):
                if p not in refs:
                    refs.append(p)
        for root, name in refs:
            if root != s["id"] and (root, s["id"], name) not in out:
                out.append((root, s["id"], name))
    return out


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


# ---------------------------------------------------------------- edge labels

LABEL_SIZE, LABEL_LINE, LABEL_PADX, LABEL_CAP = 10.0, 11.0, 7.0, 2
_NARROW = set("iljtfI.,:;'`|!()[]{}-1 ")
_WIDE = set("mwMW@%")


def text_width(s: str, size: float = LABEL_SIZE) -> float:
    """Approximate rendered width of a short label in the page's sans face. Good to a few percent, which is all the
    layout needs: it only has to reserve a gap wide enough that two labels never touch."""
    w = 0.0
    for ch in str(s):
        w += 0.30 if ch in _NARROW else (0.72 if ch.isupper() or ch in _WIDE else 0.545)
    return w * size


def _label_box(rows: list[dict]) -> dict:
    """The pill for one edge: up to LABEL_CAP value names, then a '+N' line. Width is measured, not guessed."""
    names = list(dict.fromkeys(str(r.get("label") or "value") for r in rows))
    lines = names[:LABEL_CAP]
    if len(names) > LABEL_CAP:
        lines.append(f"+{len(names) - LABEL_CAP} more")
    w = max((text_width(line) for line in lines), default=0.0) + 2 * LABEL_PADX
    return {"lines": lines, "w": w, "h": 5 + LABEL_LINE * len(lines), "rows": rows}


def _label_tip(rows: list[dict], src: str, tgt: str) -> str:
    """The hover text: the concrete values behind the label and the argument each one fed."""
    parts = []
    for r in rows[:4]:
        vals = list(r.get("values") or [])[:2]
        bit = str(r.get("label") or "value")
        if vals:
            more = len(r["values"]) - len(vals)
            bit += " " + ", ".join(_trunc(v, 20) for v in vals) + (f" +{more}" if more > 0 else "")
        if r.get("args"):
            bit += " → " + ", ".join(r["args"])
        parts.append(bit)
    return f"{src} → {tgt}: " + "; ".join(parts) + ("; …" if len(rows) > 4 else "")


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1])


def _place(points: list[tuple[float, float]], target_x: float) -> float:
    """The y where the curve crosses the middle of the gap its label lives in."""
    return min(points, key=lambda p: abs(p[0] - target_x))[1]


def _declash(labels: list[dict]) -> None:
    """Labels sharing a gap are pushed apart vertically (in place), keeping their order top to bottom."""
    by_gap: dict[int, list[dict]] = defaultdict(list)
    for lb in labels:
        by_gap[lb["gap"]].append(lb)
    for group in by_gap.values():
        group.sort(key=lambda lb: lb["y"])
        bottom = None
        for lb in group:
            if bottom is not None and lb["y"] - lb["h"] / 2 < bottom + 5:
                lb["y"] = bottom + 5 + lb["h"] / 2
            bottom = lb["y"] + lb["h"] / 2


def svg(nodes: list[dict], edges: list[tuple[str, str]], href: str = "#s-{id}",
        edge_labels: "dict[tuple[str, str], list[dict]] | None" = None) -> str:
    """Render the laid-out graph. Node dict: id, title, type, hits, items (fan-out size, optional), item_hits,
    skipped, error. Each node is wrapped in <a href> so clicking jumps to that step's evidence.

    `edge_labels` maps (source, target) to the values that moved along that edge -- rows of
    {label, name, values, args} from `crystal.app.provenance`. A labelled edge is drawn in its source's colour and
    carries a clickable pill naming what travelled; the gap between two layers widens to fit the widest pill in it,
    and pills that would collide are pushed apart, so nothing overlaps."""
    nodes, edges = layered_layout([dict(n) for n in nodes], edges)
    pos = {n["id"]: n for n in nodes}
    given = {k: v for k, v in (edge_labels or {}).items() if v}

    # 1. measure: a gap is at least as wide as the widest label that has to sit in it
    gaps: dict[int, float] = defaultdict(lambda: float(H_GAP))
    boxes: dict[tuple[str, str], dict] = {}
    for a, b in edges:
        rows = given.get((a, b))
        if not rows:
            continue
        boxes[(a, b)] = box = _label_box(rows)
        L = pos[a]["layer"]
        gaps[L] = max(gaps[L], box["w"] + 26)
    max_layer = max((n["layer"] for n in nodes), default=0)
    x_of, x = {}, float(PAD)
    for L in range(max_layer + 1):
        x_of[L] = x
        x += NODE_W + gaps[L]
    for n in nodes:
        n["x"] = x_of[n["layer"]]

    # 2. place each label where its curve crosses the middle of that gap, then resolve collisions
    paths, placed = [], []
    for a, b in edges:
        s, t = pos[a], pos[b]
        p0 = (s["x"] + NODE_W, s["y"] + NODE_H / 2)
        p3 = (t["x"], t["y"] + NODE_H / 2)
        dx = max(24.0, (p3[0] - p0[0]) / 2)
        c1, c2 = (p0[0] + dx, p0[1]), (p3[0] - dx, p3[1])
        paths.append((a, b, p0, c1, c2, p3))
        box = boxes.get((a, b))
        if box:
            L = s["layer"]
            cx = x_of[L] + NODE_W + gaps[L] / 2
            pts = [_bezier(p0, c1, c2, p3, i / 48) for i in range(49)]
            placed.append({**box, "a": a, "b": b, "gap": L, "x": cx, "y": _place(pts, cx)})
    _declash(placed)

    width = max((n["x"] for n in nodes), default=0) + NODE_W + PAD + 8
    height = max([n["y"] + NODE_H for n in nodes] + [lb["y"] + lb["h"] / 2 for lb in placed] + [0]) + PAD + 8
    moved = list(dict.fromkeys(r.get("label") for lb in placed for r in lb["rows"] if r.get("label")))
    aria = f"research flow: {len(nodes)} steps" + (f"; values moving: {', '.join(moved[:8])}" if moved else "")
    out = [f'<svg class="dag" xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{html.escape(aria)}">',
           '<defs><marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#9aa39e"/></marker>'
           '<marker id="arrv" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="currentColor"/></marker></defs>']
    for a, b, p0, c1, c2, p3 in paths:
        d = f"M{p0[0]:.0f},{p0[1]:.0f} C{c1[0]:.0f},{c1[1]:.0f} {c2[0]:.0f},{c2[1]:.0f} {p3[0]:.0f},{p3[1]:.0f}"
        if (a, b) in boxes:
            color = TYPE_COLORS.get(pos[a].get("type") or "other", TYPE_COLORS["other"])
            out.append(f'<path class="edge carries" d="{d}" fill="none" stroke="{color}" stroke-width="1.5" opacity="0.55" style="color:{color}" marker-end="url(#arrv)"/>')
        else:
            out.append(f'<path class="edge" d="{d}" fill="none" stroke="#b9c0bc" stroke-width="1.3" marker-end="url(#arr)"/>')
    for n in nodes:
        out.append(_node(n, href))
    for lb in placed:                       # labels last: they sit on top of the curves they belong to
        out.append(_edge_label(lb, pos, href))
    out.append("</svg>")
    return "\n".join(out)


def _edge_label(lb: dict, pos: dict, href: str) -> str:
    """One pill naming what moved. It links to the consuming step's evidence, exactly as a node does."""
    src, tgt = pos[lb["a"]], pos[lb["b"]]
    color = TYPE_COLORS.get(src.get("type") or "other", TYPE_COLORS["other"])
    w, h = lb["w"], lb["h"]
    tip = html.escape(_label_tip(lb["rows"], src.get("title") or src["id"], tgt.get("title") or tgt["id"]))
    link = tgt.get("href") or href.format(id=lb["b"])
    g = [f'<a href="{html.escape(link)}" class="edge-link" data-step="{html.escape(lb["b"])}">',
         f'<g class="elabel" transform="translate({lb["x"] - w / 2:.1f},{lb["y"] - h / 2:.1f})"><title>{tip}</title>',
         f'<rect width="{w:.1f}" height="{h:.1f}" rx="{min(9.0, h / 2):.1f}" fill="#ffffff" stroke="{color}" stroke-width="1"/>']
    for i, line in enumerate(lb["lines"]):
        fill = color if i < LABEL_CAP else "#5c6662"
        g.append(f'<text class="vname" x="{w / 2:.1f}" y="{LABEL_LINE * (i + 1) - 1.5:.1f}" font-size="{LABEL_SIZE}" text-anchor="middle" fill="{fill}">{html.escape(line)}</text>')
    g.append("</g></a>")
    return "".join(g)


def _node(n: dict, href: str) -> str:
    """One step: colour by information type, a hits badge, a stack behind it when the step fanned out."""
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
         f'<g class="node t-{html.escape(n.get("type") or "other")}" transform="translate({n["x"]:.0f},{n["y"]:.0f})"><title>{tip}</title>']
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
    return "".join(g)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tint(hex_color: str, alpha: float = 0.10) -> str:
    """A light tint of a hex colour over white (so fills stay readable under the label text)."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: int(round(255 + (c - 255) * alpha))  # noqa: E731
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"
