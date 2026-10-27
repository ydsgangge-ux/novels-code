"""Layered patch documents — how a runtime is composed without editing code.

A *patch* is an ordered list of three operations::

    - insert:                       # add rows, appended in order
        - id: llm
          name: gangge.runtime.plugins.llm
          config: { provider: deepseek }
    - patch:                        # retarget an existing row by id
        - id: tools
          config: { load_plugins: false }
    - remove:                       # drop rows
        - browser

Rules, copied from DeepSeek Harness on purpose:

* A ``patch`` operation **replaces the targeted row's whole config**, it does
  not deep-merge. Anything a mode needs differently belongs to that mode's
  bundle, so every row is owned by exactly one layer plus the user's overlay.
* Row **order carries no activation semantics** — activation is driven by
  service availability (``inject``), not by position. Order only decides tie
  breaks between plugins that depend on nothing.
* Later layers win. The layer order is::

      bundle 1 … bundle N → profile cordis.patch.yml → home patch → --patch
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

OPS = ("insert", "patch", "remove")


class PatchError(ValueError):
    """A patch document is malformed or targets a row that does not exist."""


@dataclass
class Row:
    """One plugin entry in the composed tree."""

    id: str
    name: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    when: str = ""
    source: str = ""            # which layer last wrote this row
    mounted: Any = None         # Mount handle, filled in by boot

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id}
        if self.name:
            out["name"] = self.name
        if self.config:
            out["config"] = self.config
        if self.disabled:
            out["disabled"] = True
        if self.when:
            out["when"] = self.when
        if self.source:
            out["source"] = self.source
        return out


# ── Loading ─────────────────────────────────────────────────────


def load_document(path: str | Path) -> list[dict[str, Any]]:
    """Load a patch document from ``.yml`` / ``.yaml`` / ``.json``."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"patch document not found: {p}")
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()

    if suffix in (".yml", ".yaml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                f"PyYAML is required to read {p.name}. Install it with `pip install pyyaml`."
            ) from exc
        data = yaml.safe_load(text) or []
    elif suffix == ".json":
        data = json.loads(text or "[]")
    else:
        raise PatchError(f"unsupported patch format '{suffix}' (use .yml/.yaml/.json)")

    if not isinstance(data, list):
        raise PatchError(f"{p.name}: a patch document must be a list of operations")
    return data


def parse_ops(
    document: Sequence[dict[str, Any]] | dict[str, Any],
    source: str = "",
) -> list[tuple[str, Any]]:
    """Flatten a document into ``[(op, payload), ...]`` with validation."""
    if isinstance(document, dict):
        document = [document]

    ops: list[tuple[str, Any]] = []
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise PatchError(f"{source}: operation #{index} must be a mapping")
        keys = [k for k in entry if k in OPS]
        if len(keys) != 1:
            raise PatchError(
                f"{source}: operation #{index} must contain exactly one of {OPS}, got {list(entry)}"
            )
        op = keys[0]
        payload = entry[op]
        if payload is None:
            payload = []
        if op == "insert":
            if not isinstance(payload, list):
                raise PatchError(f"{source}: 'insert' takes a list of rows")
            for row in payload:
                _validate_row(row, source)
        elif op == "patch":
            if not isinstance(payload, list):
                raise PatchError(f"{source}: 'patch' takes a list of row overrides")
            for row in payload:
                if not isinstance(row, dict) or "id" not in row:
                    raise PatchError(f"{source}: every 'patch' entry needs an 'id'")
        elif op == "remove":
            if isinstance(payload, str):
                payload = [payload]
            if not isinstance(payload, list):
                raise PatchError(f"{source}: 'remove' takes a list of ids")
        ops.append((op, payload))
    return ops


def _validate_row(row: Any, source: str) -> None:
    if not isinstance(row, dict):
        raise PatchError(f"{source}: inserted rows must be mappings")
    if "id" not in row:
        raise PatchError(f"{source}: every inserted row needs an 'id'")
    if not row.get("name"):
        raise PatchError(f"{source}: row '{row['id']}' needs a 'name' (dotted plugin path)")
    if not isinstance(row.get("config", {}), dict):
        raise PatchError(f"{source}: row '{row['id']}' config must be a mapping")


# ── Applying ────────────────────────────────────────────────────


class Tree:
    """The composed plugin tree: ordered rows, retargetable by id."""

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self._index: dict[str, Row] = {}

    # -- lookups --

    def get(self, row_id: str) -> Row | None:
        return self._index.get(row_id)

    def __contains__(self, row_id: str) -> bool:
        return row_id in self._index

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def ids(self) -> list[str]:
        return [r.id for r in self.rows]

    # -- operations --

    def insert(self, rows: Iterable[dict[str, Any]], source: str = "") -> None:
        for spec in rows:
            row = Row(
                id=spec["id"],
                name=spec.get("name", ""),
                config=dict(spec.get("config") or {}),
                disabled=bool(spec.get("disabled", False)),
                when=str(spec.get("when") or ""),
                source=source,
            )
            if row.id in self._index:
                raise PatchError(
                    f"{source}: duplicate row id '{row.id}' "
                    f"(already inserted by '{self._index[row.id].source}')"
                )
            self.rows.append(row)
            self._index[row.id] = row

    def patch(self, overrides: Iterable[dict[str, Any]], source: str = "") -> None:
        for spec in overrides:
            row_id = spec["id"]
            row = self._index.get(row_id)
            if row is None:
                raise PatchError(f"{source}: cannot patch unknown row '{row_id}'")
            # A config key present at all replaces the whole config, even when
            # it is empty — that is how a mode restates its configuration.
            if "config" in spec:
                row.config = dict(spec.get("config") or {})
            if "disabled" in spec:
                row.disabled = bool(spec["disabled"])
            if "when" in spec:
                row.when = str(spec.get("when") or "")
            if "name" in spec:
                row.name = str(spec["name"] or "")
            row.source = source

    def remove(self, ids: Iterable[str], source: str = "") -> None:
        for row_id in ids:
            row = self._index.pop(row_id, None)
            if row is None:
                logger.debug("%s: remove ignored, no row '%s'", source, row_id)
                continue
            self.rows.remove(row)

    def apply(self, ops: Sequence[tuple[str, Any]], source: str = "") -> None:
        for op, payload in ops:
            getattr(self, op)(payload, source)

    # -- resolution --

    def is_enabled(self, row: Row) -> bool:
        """Evaluate ``disabled`` plus the ``when`` predicate."""
        if row.disabled:
            return False
        if not row.when:
            return True
        return _eval_predicate(row.when, row)

    def enabled_rows(self) -> list[Row]:
        return [r for r in self.rows if self.is_enabled(r)]

    def to_dicts(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rows]


def _eval_predicate(expr: str, row: Row) -> bool:
    """Evaluate the tiny ``when`` language: ``env:NAME``, ``!env:NAME``, ``os:nt``.

    Deliberately not ``eval`` — a profile file is configuration, not code.
    """
    expr = expr.strip()
    negate = expr.startswith("!")
    if negate:
        expr = expr[1:].strip()

    kind, _, value = expr.partition(":")
    result: bool
    if kind == "env":
        result = bool(os.environ.get(value))
    elif kind == "os":
        result = os.name == value
    elif kind == "always":
        result = value not in ("false", "0", "no")
    else:
        logger.warning("row '%s': unknown when predicate '%s' — treated as true", row.id, expr)
        result = True
    return (not result) if negate else result


def build_tree(layers: Sequence[tuple[str, Sequence[tuple[str, Any]]]]) -> Tree:
    """Apply layers in order; the last write to a row wins."""
    tree = Tree()
    for source, ops in layers:
        tree.apply(ops, source)
    return tree
