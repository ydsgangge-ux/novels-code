"""思考强度 → 各厂商请求参数的统一映射。

每家厂商的开关长得完全不一样，而且**参数名写错不是被忽略，是 400**：

    OpenAI       reasoning_effort = minimal | low | medium | high
    Anthropic    thinking = {type: enabled, budget_tokens: N}
                 （还要求 temperature=1、max_tokens > budget_tokens）
    Qwen         enable_thinking + thinking_budget（仅 Qwen3+）
    GLM          thinking.type = enabled | disabled
                 （GLM-4.7 / 5.3 / 4.5V 强制开启，传 disabled 会报错）
    MiMo         thinking.type = enabled | disabled（只有开关，没有档位）
    SiliconFlow  enable_thinking（仅托管的 Qwen3 支持）
    Ollama       think = true/false，gpt-oss 支持 low/medium/high
    DeepSeek     没有强度参数：思考由模型决定（chat / reasoner）
    其余厂商     没有公开参数，返回 supported=False 并说明原因

调用方拿到 ``ThinkingPlan`` 后自己决定怎么用：OpenAI 兼容适配器把它塞进
``extra_body``，Anthropic 适配器用它覆盖 ``thinking`` / ``temperature``。
不支持的厂商**不会**收到任何额外字段——宁可不生效，也不要换来一个 400。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 统一档位。default = 什么都不发，沿用厂商默认（多数推理模型默认开启）。
LEVELS = ("default", "off", "low", "medium", "high")

_ALIASES = {
    "": "default",
    "auto": "default",
    "follow": "default",
    "none": "off",
    "disabled": "off",
    "disable": "off",
    "false": "off",
    "0": "off",
    "off": "off",
    "true": "medium",
    "on": "medium",
    "enabled": "medium",
    "enable": "medium",
    "1": "medium",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

_ANTHROPIC_BUDGETS = {"low": 2048, "medium": 8192, "high": 16384}
_QWEN_BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}

#: 从 base_url 反推厂商标识，兼容直接 new 适配器、不走 registry 的调用方。
_HOST_HINTS: tuple[tuple[str, str], ...] = (
    ("xiaomimimo", "mimo"),
    ("dashscope", "qwen"),
    ("bigmodel", "zhipu"),
    ("z.ai", "zhipu"),
    ("siliconflow", "siliconflow"),
    ("moonshot", "moonshot"),
    ("baichuan", "baichuan"),
    ("lingyiwanwu", "yi"),
    ("minimax", "minimax"),
    ("stepfun", "stepfun"),
    ("deepseek", "deepseek"),
    ("api.openai.com", "openai"),
    ("anthropic", "anthropic"),
    ("localhost:11434", "ollama"),
    ("127.0.0.1:11434", "ollama"),
)


@dataclass(frozen=True)
class ThinkingPlan:
    """一次请求该怎么表达"思考强度"。

    ``extra_body`` 给 OpenAI 兼容接口；``thinking``/``temperature`` 给 Anthropic；
    ``note`` 是给设置页/日志看的人话说明（比如"该厂商只有开关，没有档位"）。
    """

    supported: bool
    level: str = "default"
    extra_body: dict[str, Any] = field(default_factory=dict)
    thinking: dict[str, Any] | None = None
    temperature: float | None = None
    note: str = ""


def normalize_level(raw: str | None) -> str:
    """把用户输入/环境变量揉成统一档位；不认识的值按 default 处理。"""
    value = (raw or "").strip().lower()
    value = _ALIASES.get(value, value)
    return value if value in LEVELS else "default"


def infer_provider(base_url: str | None) -> str:
    """从 base_url 猜厂商标识；猜不出返回空串（调用方按 custom 处理）。"""
    url = (base_url or "").lower()
    for hint, provider in _HOST_HINTS:
        if hint in url:
            return provider
    return ""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """浅层递归合并：用户显式配置优先，但不能把整块 ``thinking`` 冲掉。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def plan_thinking(
    provider: str,
    model: str,
    level: str | None,
    max_tokens: int = 0,
) -> ThinkingPlan:
    """按厂商给出请求参数。不支持的厂商返回 supported=False 且不带任何字段。"""
    provider = (provider or "").strip().lower()
    model_name = (model or "").strip()
    level = normalize_level(level)

    if level == "default":
        return ThinkingPlan(True, level, note="跟随厂商默认")

    handler = _HANDLERS.get(provider)
    if handler is None:
        return ThinkingPlan(
            False,
            level,
            note=(
                f"{provider or '该厂商'} 没有公开思考强度参数；"
                "可以在 LLM_EXTRA_BODY 里手工传，或改用厂商的思考模型"
            ),
        )
    return handler(level, model_name, max_tokens)


# ── 各厂商 ────────────────────────────────────────────────────────────────

def _plan_anthropic(level: str, model: str, max_tokens: int) -> ThinkingPlan:
    lowered = model.lower()
    if not any(tag in lowered for tag in ("claude-3-7", "claude-4", "claude-opus-4",
                                          "claude-sonnet-4", "claude-haiku-4")):
        return ThinkingPlan(
            False, level, note="该 Claude 型号不支持扩展思考（需要 Claude 3.7 / 4 及以上）"
        )
    if level == "off":
        return ThinkingPlan(True, level, note="已关闭扩展思考")

    budget = _ANTHROPIC_BUDGETS[level]
    if max_tokens:
        # Anthropic 要求 max_tokens > budget_tokens，否则直接 400。
        if max_tokens <= 2048:
            return ThinkingPlan(
                False,
                level,
                note=f"单次输出上限只有 {max_tokens}，放不下扩展思考（至少 3072）",
            )
        budget = min(budget, max_tokens - 1024)
    if budget < 1024:
        return ThinkingPlan(False, level, note="可用的思考预算不足 1024 tokens")

    return ThinkingPlan(
        True,
        level,
        thinking={"type": "enabled", "budget_tokens": budget},
        # Anthropic 规定开启扩展思考时 temperature 必须为 1。
        temperature=1.0,
        note=f"扩展思考预算 {budget} tokens（max_tokens={max_tokens or '未设置'}）",
    )


def _plan_openai(level: str, model: str, max_tokens: int) -> ThinkingPlan:
    lowered = model.lower()
    if not re.match(r"^(o1|o3|o4|gpt-5)", lowered):
        return ThinkingPlan(
            False, level, note="该模型不是推理模型，思考强度无意义"
        )

    if level == "off":
        # o 系列无法真正关闭思考，只能压到最低档；gpt-5 支持 minimal。
        effort = "minimal" if lowered.startswith("gpt-5") else "low"
        note = "o 系列不支持完全关闭思考，已压到最低档"
    else:
        effort = level
        note = f"reasoning_effort={effort}"
    return ThinkingPlan(True, level, extra_body={"reasoning_effort": effort}, note=note)


def _plan_mimo(level: str, model: str, max_tokens: int) -> ThinkingPlan:
    if "instruct" in model.lower():
        return ThinkingPlan(False, level, note="该 MiMo 型号不支持深度思考")
    if level == "off":
        return ThinkingPlan(
            True,
            level,
            extra_body={"thinking": {"type": "disabled"}},
            note="已关闭深度思考",
        )
    return ThinkingPlan(
        True,
        level,
        extra_body={"thinking": {"type": "enabled"}},
        note="MiMo 只有开/关，非关闭档位效果相同",
    )


def _plan_qwen(level: str, model: str, max_tokens: int) -> ThinkingPlan:
    lowered = model.lower()
    if not ("qwen3" in lowered or "qwen4" in lowered):
        return ThinkingPlan(
            False, level, note="只有 Qwen3 / Qwen4 支持 enable_thinking"
        )
    if level == "off":
        return ThinkingPlan(
            True, level, extra_body={"enable_thinking": False}, note="已关闭思考"
        )
    return ThinkingPlan(
        True,
        level,
        extra_body={
            "enable_thinking": True,
            "thinking_budget": _QWEN_BUDGETS[level],
        },
        note=f"thinking_budget={_QWEN_BUDGETS[level]}",
    )


def _plan_siliconflow(level: str, model: str, max_tokens: int) -> ThinkingPlan:
    lowered = model.lower()
    if "qwen3" not in lowered:
        return ThinkingPlan(
            False,
            level,
            note="SiliconFlow 只有托管的 Qwen3 支持 enable_thinking",
        )
    return ThinkingPlan(
        True,
        level,
        extra_body={"enable_thinking": level != "off"},
        note="SiliconFlow 只支持开/关，没有强度档位",
    )


def _plan_zhipu(level: str, model: str, max_tokens: int) -> ThinkingPlan:
    lowered = model.lower()
    forced = ("glm-5.3", "glm-4.7", "glm-4.5v")
    togglable = ("glm-4.5", "glm-4.6", "glm-5", "glm-z1")

    if any(tag in lowered for tag in forced):
        if level == "off":
            return ThinkingPlan(
                False, level, note="该 GLM 型号强制思考，传 disabled 会直接报错"
            )
        return ThinkingPlan(
            True,
            level,
            extra_body={"thinking": {"type": "enabled"}},
            note="该 GLM 型号强制思考，档位不生效",
        )
    if not any(tag in lowered for tag in togglable):
        return ThinkingPlan(
            False, level, note="该 GLM 型号不支持 thinking 参数（需要 GLM-4.5 及以上）"
        )
    if level == "off":
        return ThinkingPlan(
            True,
            level,
            extra_body={"thinking": {"type": "disabled"}},
            note="已关闭思维链",
        )
    return ThinkingPlan(
        True,
        level,
        extra_body={"thinking": {"type": "enabled"}},
        note="GLM 只支持开/关，没有强度档位",
    )


def _plan_ollama(level: str, model: str, max_tokens: int) -> ThinkingPlan:
    lowered = model.lower()
    if "gpt-oss" in lowered:
        effort = "low" if level == "off" else level
        return ThinkingPlan(
            True,
            level,
            extra_body={"think": effort},
            note="gpt-oss 支持 low/medium/high，无法完全关闭",
        )
    if any(tag in lowered for tag in ("deepseek-r1", "qwq", "qwen3")):
        return ThinkingPlan(
            True,
            level,
            extra_body={"think": level != "off"},
            note="本地思考模型只支持开/关",
        )
    return ThinkingPlan(
        False, level, note="该本地模型没有公开的 think 参数"
    )


def _plan_deepseek(level: str, model: str, max_tokens: int) -> ThinkingPlan:
    return ThinkingPlan(
        False,
        level,
        note=(
            "DeepSeek 没有思考强度参数：思考由模型决定"
            "（deepseek-chat 不思考 / deepseek-reasoner 思考）"
        ),
    )


def _plan_unsupported(provider: str):
    def _handler(level: str, model: str, max_tokens: int) -> ThinkingPlan:
        return ThinkingPlan(
            False,
            level,
            note=f"{provider} 未公开思考强度参数；可用 LLM_EXTRA_BODY 手工传",
        )

    return _handler


_HANDLERS = {
    "anthropic": _plan_anthropic,
    "openai": _plan_openai,
    "mimo": _plan_mimo,
    "qwen": _plan_qwen,
    "siliconflow": _plan_siliconflow,
    "zhipu": _plan_zhipu,
    "ollama": _plan_ollama,
    "deepseek": _plan_deepseek,
    "moonshot": _plan_unsupported("Moonshot"),
    "baichuan": _plan_unsupported("百川"),
    "yi": _plan_unsupported("零一万物"),
    "minimax": _plan_unsupported("MiniMax"),
    "stepfun": _plan_unsupported("阶跃星辰"),
    "custom": _plan_unsupported("自定义厂商"),
}
