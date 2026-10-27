"""Argument validation for tool calls — the *before* half of robustness.

The runtime ships a repair layer (``runtime/plugins/schema_guard.py``) that
fixes near-miss arguments and explains what it could not fix. That layer is
optional (it is one bundle among several), which used to mean a bare
``ToolRegistry`` — a library embedder, a test, a host that built its own
pipeline — would call ``execute(**arguments)`` with whatever the model produced
and die on a ``TypeError`` deep inside the tool.

This module is the floor: a real JSON-Schema check performed by the registry
itself, so "the arguments match the declared schema" holds for *every* caller.
It is deliberately strict about structure (types, enums, required, nested
objects, extra keys) and never about semantics — the tool decides those.
"""

from __future__ import annotations

import json
from typing import Any

try:  # jsonschema is a hard dependency, but stay importable without it
    import jsonschema
    from jsonschema import Draft7Validator

    _HAVE_JSONSCHEMA = True
except Exception:  # pragma: no cover - degraded mode
    jsonschema = None  # type: ignore[assignment]
    Draft7Validator = None  # type: ignore[assignment,misc]
    _HAVE_JSONSCHEMA = False


_TYPE_NAMES = {
    "string": "字符串",
    "integer": "整数",
    "number": "数字",
    "boolean": "布尔值",
    "array": "数组",
    "object": "对象",
    "null": "null",
}

_MAX_PROBLEMS = 6


class ToolInputError(ValueError):
    """Raised when tool arguments do not match the tool's declared schema."""

    def __init__(self, tool: str, problems: list[str], message: str = ""):
        self.tool = tool
        self.problems = problems
        super().__init__(message or f"{tool}: " + "; ".join(problems))


def _type_expectation(schema: dict[str, Any]) -> str:
    wanted = schema.get("type")
    if isinstance(wanted, list):
        return " 或 ".join(_TYPE_NAMES.get(str(t), str(t)) for t in wanted)
    if wanted:
        return _TYPE_NAMES.get(str(wanted), str(wanted))
    return "任意"


def _describe(value: Any) -> str:
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, str):
        return f"字符串 {value[:40]!r}"
    if isinstance(value, int):
        return "整数"
    if isinstance(value, float):
        return "小数"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, dict):
        return "对象"
    if value is None:
        return "null"
    return type(value).__name__


def _fallback_validate(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Minimal structural check used when jsonschema is unavailable."""
    problems: list[str] = []
    props: dict[str, Any] = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in arguments or arguments[key] is None:
            problems.append(f'缺少必填字段 "{key}"')

    for key, value in arguments.items():
        spec = props.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                problems.append(f'未知字段 "{key}"')
            continue
        wanted = spec.get("type")
        ok = True
        if wanted == "string":
            ok = isinstance(value, str)
        elif wanted == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif wanted == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif wanted == "boolean":
            ok = isinstance(value, bool)
        elif wanted == "array":
            ok = isinstance(value, list)
        elif wanted == "object":
            ok = isinstance(value, dict)
        if not ok:
            problems.append(
                f'字段 "{key}" 应为 {_type_expectation(spec)}，实际是 {_describe(value)}'
            )
        if "enum" in spec and isinstance(value, (str, int, float, bool)):
            if value not in spec["enum"]:
                allowed = ", ".join(repr(v) for v in spec["enum"])
                problems.append(f'字段 "{key}" 只能取 {allowed}，收到 {value!r}')
    return problems


def _format_jsonschema_error(error: Any) -> str:
    """Turn a jsonschema error into one actionable Chinese sentence."""
    path = ".".join(str(p) for p in error.absolute_path)
    where = f'字段 "{path}"' if path else "参数"

    if error.validator == "required":
        missing = str(error.message).split("'")[1] if "'" in str(error.message) else "?"
        return f'缺少必填字段 "{missing}"'
    if error.validator == "additionalProperties":
        return f"{where} 含未知字段（{error.message}）"
    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, list):
            expected = " 或 ".join(_TYPE_NAMES.get(str(t), str(t)) for t in expected)
        else:
            expected = _TYPE_NAMES.get(str(expected), str(expected))
        return f"{where} 应为 {expected}，实际是 {_describe(error.instance)}"
    if error.validator == "enum":
        allowed = ", ".join(repr(v) for v in error.validator_value)
        return f"{where} 只能取 {allowed}，收到 {error.instance!r}"
    if error.validator in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        return f"{where} 超出取值范围（{error.message}）"
    if error.validator in ("minLength", "maxLength", "minItems", "maxItems"):
        return f"{where} 长度不合法（{error.message}）"
    return f"{where}: {error.message}"


def check_arguments(
    schema: dict[str, Any] | None,
    arguments: dict[str, Any] | None,
) -> list[str]:
    """Return a list of schema violations. Empty list means "valid"."""
    if not schema:
        return []
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return [f"参数必须是对象，实际是 {_describe(arguments)}"]

    if not _HAVE_JSONSCHEMA:  # pragma: no cover - degraded mode
        return _fallback_validate(schema, arguments)

    try:
        validator = Draft7Validator(schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
    except Exception:
        # A schema we cannot compile must not block the tool (many third-party
        # tools ship loose schemas); fall back to the structural check.
        return _fallback_validate(schema, arguments)

    problems: list[str] = []
    seen: set[str] = set()
    for error in errors:
        message = _format_jsonschema_error(error)
        if message in seen:
            continue
        seen.add(message)
        problems.append(message)
        if len(problems) >= _MAX_PROBLEMS:
            problems.append("…（还有更多问题，请先修正上面这些）")
            break
    return problems


def format_schema_hint(tool_name: str, schema: dict[str, Any] | None) -> str:
    """The parameter list a model needs to retry correctly."""
    if not schema:
        return ""
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    lines = [f"参数说明（{tool_name}）："]
    for key, spec in props.items():
        tag = "必填" if key in required else "可选"
        desc = str(spec.get("description") or "").split("\n")[0][:60]
        default = spec.get("default")
        extra = f"，默认 {default!r}" if default is not None else ""
        lines.append(f"  - {key}: {_type_expectation(spec)}（{tag}{extra}）{desc}")
    return "\n".join(lines)


def format_rejection(
    tool_name: str,
    schema: dict[str, Any] | None,
    arguments: dict[str, Any] | None,
    problems: list[str],
) -> str:
    """A complete, copy-paste-able correction message for the model."""
    lines = [f"❌ 参数校验失败：{tool_name}"]
    lines.extend(f"   • {p}" for p in problems)
    hint = format_schema_hint(tool_name, schema)
    if hint:
        lines.append(hint)
    try:
        echoed = json.dumps(arguments or {}, ensure_ascii=False)[:300]
    except (TypeError, ValueError):
        echoed = repr(arguments)[:300]
    lines.append(f"你实际传入：{echoed}")
    lines.append("请按上面的字段名与类型修正后重新调用，不要原样重试。")
    return "\n".join(lines)


def validate_arguments(
    tool_name: str,
    schema: dict[str, Any] | None,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and fill defaults. Raises :class:`ToolInputError`."""
    args = dict(arguments or {})
    problems = check_arguments(schema, args)
    if problems:
        raise ToolInputError(
            tool_name,
            problems,
            format_rejection(tool_name, schema, args, problems),
        )
    return apply_defaults(schema, args)


def apply_defaults(
    schema: dict[str, Any] | None, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Fill declared defaults so tools can index ``kwargs[key]`` safely."""
    if not schema:
        return arguments
    out = dict(arguments)
    for key, spec in (schema.get("properties") or {}).items():
        if key not in out and "default" in spec:
            out[key] = spec["default"]
    return out
