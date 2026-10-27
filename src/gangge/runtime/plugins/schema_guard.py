"""Schema guard — validate and repair tool arguments before execution.

Small models (GLM-Flash, Qwen 7B, local 8B) almost never fail because they can't
reason. They fail because they emit a call that is *nearly* right:

    {"file_path": "a.py"}                  → the field is called "path"
    {"command": "ls", "timeout": "30"}     → timeout must be an integer
    {"pattern": "def ", "path": [".", "x"]}→ path must be a string
    {"path": "a.py", "extra_thing": 1}     → unknown field, crashes the tool
    {"content": "```python\\nx=1\\n```"}     → model wrapped the file in a fence

A strong model recovers from the resulting ``TypeError`` on its own. A weak one
gives up, or retries with the same mistake. So the guard does two things, in
this order:

1. **Repair** what is unambiguously repairable (aliases, string→int, fences,
   drop unknown keys). The call then just succeeds — no round wasted.
2. **Explain** what cannot be repaired, in a form a small model can act on: the
   field, the expected type, the full parameter list, an echoed copy of what it
   actually sent, and a ready-to-copy example.

The failure result is a normal error tool result, never an exception, so the
turn continues and the model gets exactly one clear chance to fix itself.

This runs as a waterfall listener, so a stronger model can simply drop the
bundle — nothing here is hard-wired into the loop.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject

logger = logging.getLogger(__name__)

name = "schema_guard"
inject = Inject(required=("tools",))

# Canonical field name → spellings small models actually emit.
# Derived from the schemas this project ships, not invented.
ALIASES: dict[str, tuple[str, ...]] = {
    "path": ("file_path", "filepath", "file", "filename", "target", "target_file"),
    "command": ("cmd", "command_line", "shell_command", "script", "shell"),
    "content": ("text", "data", "body", "file_content", "contents"),
    "pattern": ("query", "regex", "search", "search_pattern", "keyword"),
    "url": ("link", "uri", "href", "address"),
    "question": ("prompt", "message", "ask"),
    "old_str": ("old_string", "old_text", "old", "search_text", "find"),
    "new_str": ("new_string", "new_text", "new", "replace_text", "replace"),
    "include": ("glob", "file_pattern", "filter"),
    "offset": ("start", "start_line", "from_line"),
    "limit": ("count", "max_lines", "lines", "num_lines"),
    "timeout": ("timeout_seconds", "time_limit"),
    "chapter": ("chapter_number", "chapter_num", "index"),
}

# Fields where a wrapping ``` fence is certainly an artefact.
FENCE_PRONE = ("content", "new_str", "old_str", "code", "text")

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\n(.*?)\n?```\s*$", re.DOTALL)

_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class SchemaGuard:
    """Repair-then-explain front door for tool arguments."""

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        self.ctx = ctx
        self.config = config or {}
        self.repairs = 0
        self.rejections = 0

    # -- public ---------------------------------------------------

    def check(self, invoke: Any, next: Any) -> Any:
        tool = self.ctx.tools.get(invoke.tool)
        if tool is None:
            return self._unknown_tool(invoke, next)

        schema = getattr(tool, "input_schema", None)
        if not schema:
            # A tool with no declared schema: nothing to validate against.
            return next(invoke)

        props: dict[str, Any] = schema.get("properties") or {}
        required: list[str] = list(schema.get("required") or [])
        given = dict(invoke.arguments or {})
        notes: list[str] = []

        args = self._apply_aliases(given, props, notes)
        args = self._coerce_values(args, props, notes)
        args = self._drop_unknown(args, props, notes)

        problems = self._validate(args, props, required)
        if problems:
            self.rejections += 1
            logger.info("[schema] rejected %s: %s", invoke.tool, "; ".join(problems))
            invoke.arguments = given  # keep the original for the audit trail
            return invoke.veto(self._explain(invoke.tool, schema, given, problems))

        if notes:
            self.repairs += 1
            invoke.metadata["schema_repairs"] = notes
            logger.info("[schema] repaired %s: %s", invoke.tool, "; ".join(notes))

        invoke.arguments = args
        return next(invoke)

    def _unknown_tool(self, invoke: Any, next: Any) -> Any:
        """Turn a hallucinated tool name into a suggestion, not a dead end.

        Weak models invent plausible names (``read`` for ``read_file``,
        ``search`` for ``grep``) and then retry the same wrong name, because
        "未知工具: read" tells them nothing actionable. Naming the closest real
        tools fixes that in one round.

        Only vetoes when there *is* a close match — an unfamiliar name may be an
        MCP tool registered later, and that must still reach the registry.
        """
        try:
            names = [t.name for t in self.ctx.tools.list_tools()]
        except Exception:
            return next(invoke)

        close = difflib.get_close_matches(invoke.tool, names, n=3, cutoff=0.55)
        if not close:
            return next(invoke)

        self.rejections += 1
        lines = [
            f"❌ 工具 \"{invoke.tool}\" 不存在。",
            "你是不是想用：" + "、".join(f"`{c}`" for c in close),
        ]
        if len(close) == 1:
            best = self.ctx.tools.get(close[0])
            example = _example(getattr(best, "input_schema", {}) or {})
            if example:
                lines.append(f"正确示例：{close[0]}({_dump(example)})")
        lines.append("请用正确的工具名重新调用，不要重复调用不存在的工具。")
        logger.info("[schema] unknown tool %r → suggested %s", invoke.tool, close)
        return invoke.veto("\n".join(lines))

    # -- repair stages --------------------------------------------

    def _apply_aliases(
        self, args: dict[str, Any], props: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        if not self.config.get("coerce", True):
            return dict(args)
        out = dict(args)
        for canonical, aliases in ALIASES.items():
            if canonical not in props or canonical in out:
                continue
            for alias in aliases:
                if alias in out:
                    out[canonical] = out.pop(alias)
                    notes.append(f"{alias}→{canonical}")
                    break
        return out

    def _coerce_values(
        self, args: dict[str, Any], props: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        if not self.config.get("coerce", True):
            return args
        out = dict(args)
        for key, value in list(out.items()):
            spec = props.get(key) or {}
            wanted = spec.get("type")
            coerced = _coerce(value, wanted)
            if coerced is not value and coerced != value:
                out[key] = coerced
                notes.append(f"{key}: {type(value).__name__}→{wanted}")
            if key in FENCE_PRONE and isinstance(out[key], str):
                stripped = _strip_fence(out[key])
                if stripped is not out[key]:
                    out[key] = stripped
                    notes.append(f"{key}: 去掉代码围栏")
        return out

    @staticmethod
    def _drop_unknown(
        args: dict[str, Any], props: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        """Unknown keys are silently dropped — a tool would crash on them."""
        extra = [key for key in args if key not in props]
        if not extra:
            return args
        for key in extra:
            args.pop(key)
        notes.append("丢弃未知字段 " + ", ".join(extra))
        return args

    # -- validation -----------------------------------------------

    @staticmethod
    def _validate(
        args: dict[str, Any], props: dict[str, Any], required: list[str]
    ) -> list[str]:
        problems: list[str] = []

        missing = [key for key in required if args.get(key) in (None, "")]
        if missing:
            problems.append("缺少必填字段: " + ", ".join(f'"{m}"' for m in missing))

        for key, value in args.items():
            spec = props.get(key)
            if not spec:
                continue
            wanted = spec.get("type")
            if wanted and not _type_ok(value, wanted):
                problems.append(
                    f'字段 "{key}" 类型应为 {wanted}，实际收到 {_describe(value)}'
                )

        return problems

    # -- explanation ----------------------------------------------

    def _explain(
        self,
        tool_name: str,
        schema: dict[str, Any],
        given: dict[str, Any],
        problems: list[str],
    ) -> str:
        """Build a correction message a small model can actually use."""
        props: dict[str, Any] = schema.get("properties") or {}
        required = set(schema.get("required") or [])

        lines = [f"❌ 参数校验失败：{tool_name}"]
        for problem in problems:
            lines.append(f"   • {problem}")

        lines.append("")
        lines.append("这个工具接受的参数：")
        for key, spec in props.items():
            tag = "必填" if key in required else "可选"
            desc = str(spec.get("description") or "").split("\n")[0][:50]
            default = spec.get("default")
            extra = f"，默认 {default!r}" if default is not None else ""
            lines.append(f"   - {key} ({spec.get('type', 'any')}, {tag}){extra}  {desc}")

        lines.append("")
        lines.append(
            "你实际传入：" + _dump(given)
        )
        example = _example(schema)
        if example:
            lines.append("正确示例：" + _dump(example))
            lines.append("请只使用上面列出的字段名，修正后重新调用这个工具。")
        return "\n".join(lines)


# ── helpers ─────────────────────────────────────────────────────


def _strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    if match:
        return match.group(1)
    return text


def _coerce(value: Any, wanted: str | None) -> Any:
    """Best-effort type repair. Returns the value unchanged when unsure."""
    if not wanted:
        return value

    if wanted == "integer" and isinstance(value, str):
        stripped = value.strip()
        try:
            return int(stripped)
        except ValueError:
            try:
                return int(float(stripped))
            except ValueError:
                return value

    if wanted == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value

    if wanted == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y", "是", "on"):
            return True
        if lowered in ("false", "0", "no", "n", "否", "off"):
            return False
        return value

    if wanted == "array" and not isinstance(value, list):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return parsed
                except ValueError:
                    pass
            return [value]
        return [value]

    if wanted == "object" and isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else value
        except ValueError:
            return value

    if wanted == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)

    return value


def _type_ok(value: Any, wanted: str) -> bool:
    expected = _TYPE_MAP.get(wanted)
    if expected is None:
        return True
    if wanted in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _describe(value: Any) -> str:
    if isinstance(value, str):
        return f"字符串 ({value[:40]!r})"
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, int):
        return "整数"
    if isinstance(value, float):
        return "小数"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, dict):
        return "对象"
    return type(value).__name__


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)[:400]
    except (TypeError, ValueError):
        return repr(value)[:400]


_PLACEHOLDERS = {
    "path": "src/main.py",
    "command": "ls -la",
    "content": "文件内容",
    "pattern": r"def \w+",
    "url": "https://example.com",
    "question": "需要用户确认的问题",
    "old_str": "要被替换的原文",
    "new_str": "替换后的新文本",
    "include": "*.py",
}


def _example(schema: dict[str, Any]) -> dict[str, Any]:
    """Synthesise a callable example from the schema's required fields."""
    props: dict[str, Any] = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    example: dict[str, Any] = {}
    for key in required:
        spec = props.get(key) or {}
        if key in _PLACEHOLDERS:
            example[key] = _PLACEHOLDERS[key]
        else:
            example[key] = _placeholder_for(spec.get("type"), key)
    # Include at most one optional field so the example stays short.
    for key, spec in props.items():
        if key in example:
            continue
        if key in _PLACEHOLDERS and spec.get("default") is None and len(example) < 3:
            example[key] = _PLACEHOLDERS[key]
            break
    return example


def _placeholder_for(wanted: str | None, key: str) -> Any:
    return {
        "string": f"<{key}>",
        "integer": 1,
        "number": 1.0,
        "boolean": True,
        "array": [],
        "object": {},
    }.get(wanted or "string", f"<{key}>")


def apply(ctx: Context, config: dict[str, Any]) -> None:
    guard = SchemaGuard(ctx, config)
    ctx.provide("schema_guard", guard)
    ctx.on(events.TOOLS_PRE_EXECUTE, guard.check, prepend=bool(config.get("prepend", True)))
    logger.info(
        "[schema] argument repair active (coerce=%s)", bool(config.get("coerce", True))
    )
