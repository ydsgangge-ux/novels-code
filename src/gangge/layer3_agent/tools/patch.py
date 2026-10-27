"""``apply_patch`` — a patch-format editing tool with fuzzy context matching.

Why a second editing tool when ``edit_file`` exists?

``edit_file`` needs the model to reproduce ``old_str`` byte-exactly, including
indentation, and to make it *unique*. In a 600-line file that is a coin flip:
one shifted space and the call fails, the model retries, and the round is
wasted. ``apply_patch`` changes the contract in three ways, all borrowed from
Codex's ``apply-patch`` crate:

1. **Patch, not replacement text.** The model writes context lines (unchanged),
   ``-`` lines (removed) and ``+`` lines (added). It never re-emits the whole
   block, so there is far less to get wrong and far fewer tokens spent.
2. **Fuzzy anchor search.** A chunk is located by its context lines allowing
   whitespace differences and a nearby offset, instead of demanding an exact
   substring. Context that drifted by a line or two still lands.
3. **Several files per call.** One patch can touch many files, which removes
   the "N files → N round trips" cost of additive work.

Deliberately *not* implemented: unified-diff line numbers (``@@ -12,7 +12,9 @@``).
Models are bad at counting lines and the counts are redundant once anchors are
fuzzy; the ``@@`` header is kept purely as a human-readable hint.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer3_agent.tools.file_ops import _compute_diff

logger = logging.getLogger(__name__)

BEGIN_PATCH = "*** Begin Patch"
END_PATCH = "*** End Patch"
ADD_FILE = "*** Add File:"
DELETE_FILE = "*** Delete File:"
UPDATE_FILE = "*** Update File:"
MOVE_TO = "*** Move to:"
END_OF_FILE = "*** End of File"

MAX_PATCH_CHARS = 400_000
FUZZY_THRESHOLD = 0.75


class PatchError(ValueError):
    """A patch that cannot be parsed or does not apply."""


@dataclass
class Chunk:
    header: str = ""
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


@dataclass
class FileOp:
    kind: str                      # add | delete | update
    path: str
    move_to: str = ""
    add_lines: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)


# ── Parsing ─────────────────────────────────────────────────────


def parse_patch(text: str) -> list[FileOp]:
    """Parse the patch body into file operations. Raises :class:`PatchError`."""
    if not text or not text.strip():
        raise PatchError("patch 为空")
    if len(text) > MAX_PATCH_CHARS:
        raise PatchError(f"patch 过大（{len(text)} 字符，上限 {MAX_PATCH_CHARS}）")

    lines = _strip_fence(text).split("\n")
    start = _index_of(lines, BEGIN_PATCH)
    if start < 0:
        raise PatchError(f"缺少 `{BEGIN_PATCH}` 开头行")
    end = _index_of(lines, END_PATCH, start)
    if end < 0:
        raise PatchError(f"缺少 `{END_PATCH}` 结尾行")

    ops: list[FileOp] = []
    i = start + 1
    while i < end:
        raw = lines[i]
        stripped = raw.strip()

        if not stripped:
            i += 1
            continue
        if stripped.startswith(ADD_FILE):
            path = stripped[len(ADD_FILE):].strip()
            i += 1
            added: list[str] = []
            while i < end and not _is_marker(lines[i]):
                body = lines[i]
                if body.startswith("+"):
                    added.append(body[1:])
                elif body.strip():
                    raise PatchError(
                        f"Add File {path} 的正文行必须以 '+' 开头：{body[:60]!r}"
                    )
                i += 1
            ops.append(FileOp(kind="add", path=path, add_lines=added))
            continue
        if stripped.startswith(DELETE_FILE):
            ops.append(FileOp(kind="delete", path=stripped[len(DELETE_FILE):].strip()))
            i += 1
            continue
        if stripped.startswith(UPDATE_FILE):
            op = FileOp(kind="update", path=stripped[len(UPDATE_FILE):].strip())
            i += 1
            if i < end and lines[i].strip().startswith(MOVE_TO):
                op.move_to = lines[i].strip()[len(MOVE_TO):].strip()
                i += 1
            op.chunks, i = _parse_chunks(lines, i, end)
            if not op.chunks:
                raise PatchError(f"Update File {op.path} 没有任何修改块")
            ops.append(op)
            continue

        raise PatchError(f"无法识别的 patch 行: {raw[:80]!r}")

    if not ops:
        raise PatchError("patch 中没有任何文件操作")
    return ops


def _parse_chunks(lines: list[str], i: int, end: int) -> tuple[list[Chunk], int]:
    chunks: list[Chunk] = []
    current: Chunk | None = None
    while i < end and not _is_marker(lines[i], allow_eof=False):
        line = lines[i]
        if line.startswith("@@"):
            current = Chunk(header=line[2:].strip())
            chunks.append(current)
            i += 1
            continue
        if current is None:
            current = Chunk()
            chunks.append(current)

        if line.startswith("+"):
            current.new.append(line[1:])
        elif line.startswith("-"):
            current.old.append(line[1:])
        elif line.startswith(" ") or line == "":
            content = line[1:] if line.startswith(" ") else ""
            current.old.append(content)
            current.new.append(content)
        elif line.strip() == END_OF_FILE:
            i += 1
            break
        else:
            # A line with no prefix is treated as context — models routinely
            # drop the leading space, and refusing costs a whole round.
            current.old.append(line)
            current.new.append(line)
        i += 1
    return [c for c in chunks if c.old or c.new], i


def _is_marker(line: str, allow_eof: bool = True) -> bool:
    stripped = line.strip()
    if any(
        stripped.startswith(m)
        for m in (ADD_FILE, DELETE_FILE, UPDATE_FILE, BEGIN_PATCH, END_PATCH)
    ):
        return True
    return allow_eof and stripped == END_OF_FILE


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        return body
    return text


def _index_of(lines: list[str], needle: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if lines[i].strip() == needle:
            return i
    return -1


# ── Anchoring ───────────────────────────────────────────────────


def seek_sequence(haystack: list[str], pattern: list[str], start: int = 0) -> tuple[int, str]:
    """Locate ``pattern`` in ``haystack`` starting at ``start``.

    Returns ``(index, strategy)`` where strategy is ``exact`` / ``trimmed`` /
    ``fuzzy`` / ``empty``; index is -1 when not found.
    """
    if not pattern:
        return len(haystack), "empty"
    limit = len(haystack) - len(pattern)
    if limit < 0:
        return -1, "missing"

    for i in range(start, limit + 1):
        if haystack[i : i + len(pattern)] == pattern:
            return i, "exact"

    trimmed = [line.strip() for line in pattern]
    for i in range(start, limit + 1):
        if [line.strip() for line in haystack[i : i + len(pattern)]] == trimmed:
            return i, "trimmed"

    best_index, best_ratio = -1, 0.0
    for i in range(start, limit + 1):
        window = haystack[i : i + len(pattern)]
        ratio = difflib.SequenceMatcher(
            None, "\n".join(trimmed), "\n".join(line.strip() for line in window)
        ).ratio()
        if ratio > best_ratio:
            best_ratio, best_index = ratio, i
    if best_ratio >= FUZZY_THRESHOLD:
        return best_index, "fuzzy"
    return -1, "missing"


# ── Application ─────────────────────────────────────────────────


def apply_file_ops(ops: list[FileOp], workspace: Path) -> list[dict[str, Any]]:
    """Apply parsed ops to disk. Returns per-file records (path, diff, counts)."""
    records: list[dict[str, Any]] = []
    for op in ops:
        target = _resolve(op.path, workspace)
        if op.kind == "add":
            records.append(_apply_add(op, target))
        elif op.kind == "delete":
            records.append(_apply_delete(op, target))
        else:
            records.append(_apply_update(op, target, workspace))
    return records


def _apply_add(op: FileOp, target: Path) -> dict[str, Any]:
    if target.exists():
        raise PatchError(
            f"{op.path} 已存在；修改已有文件请用 `*** Update File:`，"
            "整体重写请用 write_file"
        )
    content = "\n".join(op.add_lines) + "\n" if op.add_lines else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "path": op.path,
        "kind": "add",
        "added": len(op.add_lines),
        "removed": 0,
        "diff": _compute_diff("", content, op.path),
        "before_content": "",
        "after_content": content[:5000],
    }


def _apply_delete(op: FileOp, target: Path) -> dict[str, Any]:
    if not target.exists():
        raise PatchError(f"要删除的文件不存在: {op.path}")
    before = target.read_text(encoding="utf-8", errors="replace")
    target.unlink()
    return {
        "path": op.path,
        "kind": "delete",
        "added": 0,
        "removed": before.count("\n") + 1,
        "diff": _compute_diff(before, "", op.path),
        "before_content": before[:5000],
        "after_content": "",
    }


def _apply_update(op: FileOp, target: Path, workspace: Path) -> dict[str, Any]:
    if not target.exists():
        raise PatchError(
            f"要修改的文件不存在: {op.path}（如需新建请用 `*** Add File:`）"
        )
    original = target.read_text(encoding="utf-8", errors="replace")
    trailing_newline = original.endswith("\n")
    lines = original.split("\n")
    if trailing_newline:
        lines = lines[:-1]

    added = removed = 0
    cursor = 0
    for chunk in op.chunks:
        position, strategy = seek_sequence(lines, chunk.old, cursor)
        if position < 0:
            hint = "\n".join(chunk.old[:8])
            raise PatchError(
                f"{op.path} 的补丁块无法定位（上下文与文件内容不匹配）。\n"
                f"期望的上下文片段:\n{hint}\n"
                "请先用 read_file 查看当前内容，再按实际文本重写该块。"
            )
        logger.debug("[patch] %s chunk at %s (%s)", op.path, position, strategy)
        lines[position : position + len(chunk.old)] = chunk.new
        cursor = position + len(chunk.new)
        removed += len(chunk.old)
        added += len(chunk.new)

    new_content = "\n".join(lines)
    if trailing_newline:
        new_content += "\n"

    final_path = _resolve(op.move_to, workspace) if op.move_to else target
    if op.move_to:
        if final_path.exists():
            raise PatchError(f"移动目标已存在: {op.move_to}")
        final_path.parent.mkdir(parents=True, exist_ok=True)

    final_path.write_text(new_content, encoding="utf-8")
    if op.move_to:
        target.unlink()

    return {
        "path": op.move_to or op.path,
        "kind": "update",
        "added": added,
        "removed": removed,
        "diff": _compute_diff(original, new_content, op.path),
        "before_content": original[:5000],
        "after_content": new_content[:5000],
    }


def _resolve(path: str, workspace: Path) -> Path:
    if not path:
        raise PatchError("文件路径为空")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / path
    try:
        resolved = candidate.resolve()
        resolved.relative_to(workspace.resolve())
    except ValueError as exc:
        raise PatchError(f"拒绝操作工作区之外的路径: {path}") from exc
    except OSError as exc:  # pragma: no cover - exotic filesystem errors
        raise PatchError(f"无法解析路径 {path}: {exc}") from exc
    return resolved


# ── Tool ────────────────────────────────────────────────────────


class ApplyPatchTool(BaseTool):
    """Apply a multi-file patch written in the ``*** Begin Patch`` format."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace or "."

    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "用补丁方式修改/新增/删除文件，一次可处理多个文件。相比 edit_file 更省 token，"
            "且上下文只需给出大致内容（允许空格差异和位置偏移）。\n"
            "格式（注意每行前缀）：\n"
            "*** Begin Patch\n"
            "*** Update File: src/main.py\n"
            "@@ def main():\n"
            "     print('hi')        ← 空格开头表示未改动的上下文行\n"
            "-    return 1           ← '-' 表示删除这一行\n"
            "+    return 0           ← '+' 表示新增这一行\n"
            "*** Add File: src/new.py\n"
            "+print('new')\n"
            "*** Delete File: old.py\n"
            "*** End Patch\n"
            "规则：Update 必须带至少 2 行上下文以便定位；新增文件每行都要以 '+' 开头；"
            "不要给出行号（@@ 后面写内容提示即可）。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "完整的补丁文本，以 *** Begin Patch 开头、*** End Patch 结尾",
                }
            },
            "required": ["patch"],
        }

    @property
    def tags(self) -> tuple[str, ...]:
        return ("补丁", "批量修改", "多文件", "patch", "diff", "edit")

    async def execute(self, patch: str = "", **kwargs: Any) -> ToolResult:
        if not patch:
            patch = (
                kwargs.get("input")
                or kwargs.get("diff")
                or kwargs.get("text")
                or ""
            )
        if not patch:
            return ToolResult(output="❌ apply_patch 缺少 patch 参数", is_error=True)

        workspace = Path(self.workspace).resolve()
        try:
            ops = parse_patch(patch)
        except PatchError as exc:
            return ToolResult(output=f"❌ 补丁解析失败: {exc}", is_error=True)

        try:
            records = apply_file_ops(ops, workspace)
        except PatchError as exc:
            return ToolResult(output=f"❌ 补丁应用失败: {exc}", is_error=True)
        except OSError as exc:
            return ToolResult(output=f"❌ 文件操作失败: {exc}", is_error=True)

        total_added = sum(r["added"] for r in records)
        total_removed = sum(r["removed"] for r in records)
        summary = [f"✅ 补丁已应用: {len(records)} 个文件 (+{total_added} / -{total_removed} 行)"]
        for record in records:
            summary.append(
                f"  - {record['path']} ({record['kind']}, +{record['added']} / -{record['removed']})"
            )

        return ToolResult(
            output="\n".join(summary),
            metadata={
                "diff": "\n".join(r["diff"] for r in records if r["diff"]),
                "before_content": records[0]["before_content"] if len(records) == 1 else "",
                "after_content": records[0]["after_content"] if len(records) == 1 else "",
                "files": [r["path"] for r in records],
                "lines_added": total_added,
                "lines_removed": total_removed,
            },
        )
