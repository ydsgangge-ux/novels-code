"""
叙事知识图谱 — SQLite Schema 与数据库管理

借鉴 CodeGraph 的架构：
  - nodes 表 → 叙事节点（角色/地点/事件/伏笔/物品）
  - edges 表 → 叙事边（因果关系/角色关系/事件触发/伏笔埋收/位置变化）
  - FTS5 全文搜索 → 叙事内容检索
  - schema_versions → 版本追踪与迁移
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_V1 = """\
-- 叙事知识图谱 Schema v1

CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL,
    description TEXT
);

INSERT INTO schema_versions (version, applied_at, description)
VALUES (1, strftime('%s','now') * 1000, 'Initial narrative graph schema');

-- ═══════════════════════════════════════════════════════════════
-- 叙事节点
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    chapter INT NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
);

-- ═══════════════════════════════════════════════════════════════
-- 叙事边
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    chapter INT NOT NULL DEFAULT 0,
    weight REAL NOT NULL DEFAULT 1.0,
    description TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target) REFERENCES nodes(id) ON DELETE CASCADE
);

-- ═══════════════════════════════════════════════════════════════
-- 章节内容索引
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS chapters (
    number INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    indexed_at INTEGER NOT NULL DEFAULT 0
);

-- ═══════════════════════════════════════════════════════════════
-- 索引
-- ═══════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_chapter ON nodes(chapter);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_source_kind ON edges(source, kind);
CREATE INDEX IF NOT EXISTS idx_edges_target_kind ON edges(target, kind);
CREATE INDEX IF NOT EXISTS idx_edges_chapter ON edges(chapter);

-- ═══════════════════════════════════════════════════════════════
-- FTS5 全文搜索
-- ═══════════════════════════════════════════════════════════════

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    id,
    name,
    description,
    kind,
    content='nodes',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, id, name, description, kind)
    VALUES (NEW.rowid, NEW.id, NEW.name, NEW.description, NEW.kind);
END;

CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, id, name, description, kind)
    VALUES ('delete', OLD.rowid, OLD.id, OLD.name, OLD.description, OLD.kind);
END;

CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, id, name, description, kind)
    VALUES ('delete', OLD.rowid, OLD.id, OLD.name, OLD.description, OLD.kind);
    INSERT INTO nodes_fts(rowid, id, name, description, kind)
    VALUES (NEW.rowid, NEW.id, NEW.name, NEW.description, NEW.kind);
END;
"""


class NarrativeGraphDB:
    """
    叙事知识图谱数据库。

    每本书一个 SQLite 文件，位于 books/{book_id}/narrative_graph.db
    """

    NODE_KINDS = {"character", "location", "event", "hook", "item", "faction", "thread"}
    EDGE_KINDS = {
        "causes",
        "participates",
        "located_at",
        "foreshadows",
        "resolves",
        "relationship",
        "travels_to",
        "possesses",
        "belongs_to",
        "opposes",
        "allies_with",
        "triggers",
        "aware_of",
    }

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._connect()

    def initialize(self) -> None:
        conn = self._connect()
        conn.executescript(SCHEMA_V1)
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get_schema_version(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(version) as v FROM schema_versions"
            ).fetchone()
            return row["v"] if row and row["v"] else 0
        except sqlite3.OperationalError:
            return 0

    # ── 节点操作 ──────────────────────────────────────────────────────────────

    def upsert_node(
        self,
        node_id: str,
        kind: str,
        name: str,
        description: str = "",
        chapter: int = 0,
        metadata: dict | None = None,
    ) -> None:
        conn = self._connect()
        conn.execute(
            """INSERT INTO nodes (id, kind, name, description, chapter, metadata, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now') * 1000)
               ON CONFLICT(id) DO UPDATE SET
                   kind=excluded.kind,
                   name=excluded.name,
                   description=excluded.description,
                   chapter=excluded.chapter,
                   metadata=excluded.metadata,
                   updated_at=strftime('%s','now') * 1000
            """,
            (node_id, kind, name, description, chapter, json.dumps(metadata or {}, ensure_ascii=False)),
        )
        conn.commit()

    def get_node(self, node_id: str) -> dict | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["metadata"] = json.loads(d.get("metadata", "{}"))
        return d

    def get_nodes_by_kind(self, kind: str) -> list[dict]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM nodes WHERE kind = ? ORDER BY chapter", (kind,)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata", "{}"))
            result.append(d)
        return result

    def get_all_nodes(self, kind: str | None = None) -> list[dict]:
        conn = self._connect()
        if kind:
            rows = conn.execute("SELECT * FROM nodes WHERE kind = ? ORDER BY chapter", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM nodes ORDER BY kind, chapter").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata", "{}"))
            result.append(d)
        return result

    def delete_node(self, node_id: str) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.commit()

    # ── 边操作 ──────────────────────────────────────────────────────────────

    def add_edge(
        self,
        source: str,
        target: str,
        kind: str,
        chapter: int = 0,
        weight: float = 1.0,
        description: str = "",
        metadata: dict | None = None,
    ) -> int:
        conn = self._connect()
        cursor = conn.execute(
            """INSERT INTO edges (source, target, kind, chapter, weight, description, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (source, target, kind, chapter, weight, description, json.dumps(metadata or {}, ensure_ascii=False)),
        )
        conn.commit()
        return cursor.lastrowid

    def get_outgoing_edges(self, node_id: str, kinds: list[str] | None = None) -> list[dict]:
        conn = self._connect()
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            rows = conn.execute(
                f"SELECT * FROM edges WHERE source = ? AND kind IN ({placeholders}) ORDER BY chapter",
                [node_id] + list(kinds),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edges WHERE source = ? ORDER BY chapter", (node_id,)
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata", "{}"))
            result.append(d)
        return result

    def get_incoming_edges(self, node_id: str, kinds: list[str] | None = None) -> list[dict]:
        conn = self._connect()
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            rows = conn.execute(
                f"SELECT * FROM edges WHERE target = ? AND kind IN ({placeholders}) ORDER BY chapter",
                [node_id] + list(kinds),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edges WHERE target = ? ORDER BY chapter", (node_id,)
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata", "{}"))
            result.append(d)
        return result

    def get_edges_by_kind(self, kind: str) -> list[dict]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM edges WHERE kind = ? ORDER BY chapter", (kind,)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata", "{}"))
            result.append(d)
        return result

    def get_edges_between(self, source: str, target: str, kind: str | None = None) -> list[dict]:
        conn = self._connect()
        if kind:
            rows = conn.execute(
                "SELECT * FROM edges WHERE source = ? AND target = ? AND kind = ? ORDER BY chapter",
                (source, target, kind),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edges WHERE source = ? AND target = ? ORDER BY chapter",
                (source, target),
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata", "{}"))
            result.append(d)
        return result

    def delete_edge(self, edge_id: int) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
        conn.commit()

    # ── 章节索引 ──────────────────────────────────────────────────────────────

    def upsert_chapter(self, number: int, title: str, word_count: int, content_hash: str) -> None:
        conn = self._connect()
        conn.execute(
            """INSERT INTO chapters (number, title, word_count, content_hash, indexed_at)
               VALUES (?, ?, ?, ?, strftime('%s','now') * 1000)
               ON CONFLICT(number) DO UPDATE SET
                   title=excluded.title,
                   word_count=excluded.word_count,
                   content_hash=excluded.content_hash,
                   indexed_at=strftime('%s','now') * 1000
            """,
            (number, title, word_count, content_hash),
        )
        conn.commit()

    def get_indexed_chapters(self) -> list[dict]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM chapters ORDER BY number").fetchall()
        return [dict(row) for row in rows]

    def is_chapter_indexed(self, number: int, content_hash: str) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT content_hash FROM chapters WHERE number = ?", (number,)
        ).fetchone()
        return row is not None and row["content_hash"] == content_hash

    # ── FTS5 搜索 ──────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        escaped = query.replace('"', '""')
        rows = conn.execute(
            """SELECT n.* FROM nodes_fts f
               JOIN nodes n ON n.id = f.id
               WHERE nodes_fts MATCH ?
               ORDER BY rank
               LIMIT ?
            """,
            (f'"{escaped}"', limit),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata", "{}"))
            result.append(d)
        return result

    # ── 统计 ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        conn = self._connect()
        node_count = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        edge_count = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        chapter_count = conn.execute("SELECT COUNT(*) as c FROM chapters").fetchone()["c"]
        node_kinds = {}
        for row in conn.execute("SELECT kind, COUNT(*) as c FROM nodes GROUP BY kind"):
            node_kinds[row["kind"]] = row["c"]
        edge_kinds = {}
        for row in conn.execute("SELECT kind, COUNT(*) as c FROM edges GROUP BY kind"):
            edge_kinds[row["kind"]] = row["c"]
        return {
            "nodes": node_count,
            "edges": edge_count,
            "chapters_indexed": chapter_count,
            "node_kinds": node_kinds,
            "edge_kinds": edge_kinds,
        }
