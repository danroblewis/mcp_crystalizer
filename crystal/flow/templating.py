"""Jinja environment for flow args: filters that encode the agent's small string habits."""
from __future__ import annotations

import re
import shlex
from typing import Any

from jinja2 import Environment, StrictUndefined, Undefined

_STOP = {"the", "a", "an", "of", "for", "in", "on", "at", "to", "and", "or", "is", "was", "not", "with", "within", "did", "after", "before"}


def tokens(s: Any, min_len: int = 3, keep_stop: bool = False) -> list[str]:
    """Search-worthy tokens of a string: drop punctuation, short words and stop words, keep order/dedupe."""
    out, seen = [], set()
    for t in re.findall(r"[A-Za-z0-9_.-]+", str(s or "")):
        t2 = t.strip(".-_")
        if len(t2) < min_len or (t2.lower() in _STOP and not keep_stop):
            continue
        if t2.lower() not in seen:
            seen.add(t2.lower())
            out.append(t2)
    return out


def quote(s: Any) -> str:
    return '"' + str(s).replace('"', '\\"') + '"'


def join_and(items) -> str:
    return " AND ".join(str(i) for i in items)


def join_or(items) -> str:
    return " OR ".join(str(i) for i in items)


def join_space(items) -> str:
    return " ".join(str(i) for i in items)


def short(s: Any, n: int = 10) -> str:
    return str(s)[:n]


def first(items, default=""):
    return items[0] if isinstance(items, list) and items else (items if not isinstance(items, list) else default)


def head(items, n: int = 5):
    return list(items)[:n] if isinstance(items, (list, tuple)) else items


def lucene_escape(s: Any) -> str:
    return re.sub(r'([+\-=&|><!(){}\[\]^"~*?:\\/])', r"\\\1", str(s))


def snake(s: Any) -> str:
    return str(s).replace("-", "_")


def kebab(s: Any) -> str:
    return str(s).replace("_", "-")


def date_only(s: Any) -> str:
    return str(s)[:10]


class SilentUndefined(Undefined):
    """Missing values render as '' so ladder rungs that need an absent value produce a blank
    (and are skipped), the korrel8r rule."""
    def __str__(self):
        return ""

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    __getattr__ = lambda self, name: SilentUndefined()  # noqa: E731
    __getitem__ = lambda self, name: SilentUndefined()  # noqa: E731


def make_env() -> Environment:
    env = Environment(undefined=SilentUndefined, autoescape=False, keep_trailing_newline=False)
    env.filters.update({"tokens": tokens, "quote": quote, "and": join_and, "or": join_or, "space": join_space,
                        "short": short, "first": first, "head": head, "lucene": lucene_escape, "snake": snake,
                        "kebab": kebab, "date": date_only, "shq": shlex.quote})
    return env


ENV = make_env()


def render(value: Any, ctx: dict) -> Any:
    """Render templates inside strings, lists and dicts. Non-string leaves pass through."""
    if isinstance(value, str):
        if "{{" not in value and "{%" not in value:
            return value
        return ENV.from_string(value).render(**ctx)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    return value
