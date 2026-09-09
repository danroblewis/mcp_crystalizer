"""Entity catalog (the foreign-key hub) and a gazetteer that finds catalog names in free text."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from crystal import PROJECT_ROOT

CATALOG_PATH = PROJECT_ROOT / "catalog" / "entities.yaml"


def load_catalog(path: Path | None = None) -> dict:
    path = path or CATALOG_PATH
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


class Gazetteer:
    """Finds mentions of known entity names (and aliases) in text. Longest match wins; case-insensitive;
    '-' and '_' are treated as equivalent so `payments_api` matches `payments-api`."""

    def __init__(self, entities: dict[str, dict]):
        self.entities = entities
        alts = []
        self._canon = {}
        for name, e in entities.items():
            for alias in [name, *e.get("aliases", [])]:
                key = self._norm(alias)
                self._canon[key] = name
                alts.append(re.escape(alias).replace(r"\-", "[-_]").replace("_", "[-_]"))
        alts.sort(key=len, reverse=True)
        self._rx = re.compile(r"(?<![\w-])(" + "|".join(alts) + r")(?![\w-])", re.I) if alts else None

    @staticmethod
    def _norm(s: str) -> str:
        return s.lower().replace("_", "-")

    def find(self, text: str) -> list[dict]:
        if not self._rx:
            return []
        out, seen = [], set()
        for m in self._rx.finditer(text or ""):
            name = self._canon.get(self._norm(m.group(1)))
            if name and name not in seen:
                seen.add(name)
                out.append({"name": name, "start": m.start(), "end": m.end(), "entity": self.entities[name]})
        return out


def gazetteer_for(kind: str, catalog: dict | None = None) -> Gazetteer:
    catalog = catalog if catalog is not None else load_catalog()
    return Gazetteer(catalog.get(kind, {}))
