"""Token pricing estimation for supported LLM providers.

Pricing source: https://api-docs.deepseek.com/quick_start/pricing/
DeepSeek 2026年4月更新：官方已按 1M tokens 计费。
此处换算为 每千token 价格以便计算。

注意：实际费用比计算值通常更低，因为：
- DeepSeek 缓存命中价仅 $0.0028/1M（比正价便宜 50 倍）
- 系统提示和工具定义等重复内容大部分命中缓存

三层兜底，保证**任何模型都有数**：

1. 精确命中（provider + model 完全一致）
2. 变体/前缀命中（``glm-4.7-flash`` → ``glm-4-flash``，``xxx-v2`` → ``xxx``）
3. provider 默认价 → 全局兜底价

命中 1 显示 ``¥0.145``，其余显示 ``~¥0.145`` —— 一个带波浪号的估算，
好过某些模型干脆不显示费用。想校准就放一份 ``.gangge/pricing.json``。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Per-1K-token pricing (originally per-1M, divided by 1000 for calculation)
PRICING_TABLE = {
    "deepseek": {
        # Source: https://api-docs.deepseek.com/quick_start/pricing/
        # Cache-miss prices (cache-hit is 50x cheaper, not included here)
        "deepseek-chat":     {"input_cny": 0.001, "output_cny": 0.002},   # = $0.14/$0.28 per 1M
        "deepseek-reasoner": {"input_cny": 0.004, "output_cny": 0.016},   # = $0.55/$2.19 per 1M
        "deepseek-v4-flash": {"input_cny": 0.001, "output_cny": 0.002},   # = $0.14/$0.28 per 1M
        "deepseek-v4-pro":   {"input_cny": 0.003, "output_cny": 0.006},   # = $0.435/$0.87 per 1M (75% discount)
    },
    "openai": {
        "gpt-5.5": {"input_usd": 0.0025, "output_usd": 0.01},
        "gpt-5.5-pro": {"input_usd": 0.005, "output_usd": 0.02},
        "gpt-5.5-instant": {"input_usd": 0.001, "output_usd": 0.004},
        "gpt-5.4": {"input_usd": 0.0025, "output_usd": 0.01},
        "gpt-4o": {"input_usd": 0.0025, "output_usd": 0.01},
        "gpt-4o-mini": {"input_usd": 0.00015, "output_usd": 0.0006},
        "gpt-4-turbo": {"input_usd": 0.01, "output_usd": 0.03},
        "gpt-3.5-turbo": {"input_usd": 0.0005, "output_usd": 0.0015},
        "o4-mini": {"input_usd": 0.0011, "output_usd": 0.0044},
        "o3-mini": {"input_usd": 0.0011, "output_usd": 0.0044},
    },
    "anthropic": {
        "claude-opus-4-7-20260601": {"input_usd": 0.015, "output_usd": 0.075},
        "claude-opus-4-6-20260515": {"input_usd": 0.015, "output_usd": 0.075},
        "claude-sonnet-4-6-20260501": {"input_usd": 0.003, "output_usd": 0.015},
        "claude-haiku-4-5-20260301": {"input_usd": 0.0008, "output_usd": 0.004},
        "claude-sonnet-4-20250514": {"input_usd": 0.003, "output_usd": 0.015},
        "claude-3-5-sonnet-20241022": {"input_usd": 0.003, "output_usd": 0.015},
        "claude-3-5-haiku-20241022": {"input_usd": 0.0008, "output_usd": 0.004},
        "claude-3-opus-20240229": {"input_usd": 0.015, "output_usd": 0.075},
    },
    "ollama": {
        "__default__": {"input_cny": 0, "output_cny": 0},
    },
    "qwen": {
        "__default__": {"input_cny": 0.002, "output_cny": 0.006},
        "qwen-turbo": {"input_cny": 0.0003, "output_cny": 0.0006},
        "qwen-plus": {"input_cny": 0.0008, "output_cny": 0.002},
        "qwen-max": {"input_cny": 0.002, "output_cny": 0.006},
        "qwen-long": {"input_cny": 0.0005, "output_cny": 0.002},
        "qwen-coder": {"input_cny": 0.001, "output_cny": 0.002},
    },
    "zhipu": {
        "__default__": {"input_cny": 0.001, "output_cny": 0.001},
        "glm-4-plus": {"input_cny": 0.05, "output_cny": 0.05},
        "glm-4": {"input_cny": 0.1, "output_cny": 0.1},
        "glm-4-air": {"input_cny": 0.001, "output_cny": 0.001},
        "glm-4-flash": {"input_cny": 0.0001, "output_cny": 0.0001},
        "glm-4.5": {"input_cny": 0.0006, "output_cny": 0.0022},
        "glm-4.6": {"input_cny": 0.0006, "output_cny": 0.0022},
    },
    "moonshot": {
        "__default__": {"input_cny": 0.012, "output_cny": 0.012},
        "moonshot-v1-8k": {"input_cny": 0.012, "output_cny": 0.012},
        "moonshot-v1-32k": {"input_cny": 0.024, "output_cny": 0.024},
        "moonshot-v1-128k": {"input_cny": 0.06, "output_cny": 0.06},
        "kimi-k2": {"input_cny": 0.004, "output_cny": 0.016},
    },
    "baichuan": {
        "__default__": {"input_cny": 0.004, "output_cny": 0.008},
    },
    "yi": {
        "__default__": {"input_cny": 0.005, "output_cny": 0.005},
    },
    "minimax": {
        "__default__": {"input_cny": 0.001, "output_cny": 0.002},
    },
    "stepfun": {
        "__default__": {"input_cny": 0.005, "output_cny": 0.02},
    },
    "siliconflow": {
        "__default__": {"input_cny": 0.001, "output_cny": 0.002},
    },
    # 走到这里的通常是"OpenAI 兼容 + 自建网关"的聚合商。
    "mimo": {
        "__default__": {"input_cny": 0.001, "output_cny": 0.002},
    },
    "custom": {
        "__default__": {"input_cny": 0.002, "output_cny": 0.006},
    },
}

# Default pricing for models not explicitly listed
DEFAULT_MODEL_PRICING = {
    "deepseek": {"input_cny": 0.001, "output_cny": 0.002},
    "openai": {"input_usd": 0.0025, "output_usd": 0.01},
    "anthropic": {"input_usd": 0.003, "output_usd": 0.015},
    "ollama": {"input_cny": 0, "output_cny": 0},
    "qwen": {"input_cny": 0.002, "output_cny": 0.006},
    "zhipu": {"input_cny": 0.001, "output_cny": 0.001},
    "moonshot": {"input_cny": 0.012, "output_cny": 0.012},
    "baichuan": {"input_cny": 0.004, "output_cny": 0.008},
    "yi": {"input_cny": 0.005, "output_cny": 0.005},
    "minimax": {"input_cny": 0.001, "output_cny": 0.002},
    "stepfun": {"input_cny": 0.005, "output_cny": 0.02},
    "siliconflow": {"input_cny": 0.001, "output_cny": 0.002},
}

#: 连 provider 都认不出来时用的价。取国内中等价位，宁可略高也不要显示 0
#: —— 显示 ¥0 会让人以为免费，实际账单不是这么回事。
UNKNOWN_PROVIDER_PRICING = {"input_cny": 0.002, "output_cny": 0.006}

#: 用户自定义定价：``GANGGE_PRICING_FILE`` 或 ``<cwd>/.gangge/pricing.json``。
#: 格式：``{"provider": {"model": {"input_cny": 0.001, "output_cny": 0.002}}}``
_CUSTOM_CACHE: dict | None = None


def _load_custom_pricing() -> dict:
    global _CUSTOM_CACHE
    if _CUSTOM_CACHE is not None:
        return _CUSTOM_CACHE

    candidates: list[Path] = []
    env_path = os.environ.get("GANGGE_PRICING_FILE", "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(Path.cwd() / ".gangge" / "pricing.json")

    merged: dict = {}
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for provider, models in data.items():
                    if isinstance(models, dict):
                        merged.setdefault(provider.lower(), {}).update(models)
        except Exception:  # noqa: BLE001 - 坏配置不该影响主流程
            logger.debug("[pricing] 读取自定义定价失败: %s", path, exc_info=True)

    _CUSTOM_CACHE = merged
    return merged


def _merge_pricing() -> dict:
    """内置表 + 自定义覆盖。"""
    custom = _load_custom_pricing()
    if not custom:
        return PRICING_TABLE
    merged = {k: dict(v) for k, v in PRICING_TABLE.items()}
    for provider, models in custom.items():
        merged.setdefault(provider, {}).update(models)
    return merged


def _variants(model: str) -> list[str]:
    """``glm-4.7-flash`` 的候选写法：原文 + 去掉版本号小位（→ ``glm-4-flash``）。"""
    out = [model]
    parts = model.split("-")
    for i, part in enumerate(parts):
        if "." in part:
            head = part.split(".")[0]
            candidate = "-".join(parts[:i] + [head] + parts[i + 1:])
            if candidate not in out:
                out.append(candidate)
    return out


def _lookup(table: dict, provider: str, model: str) -> tuple[dict | None, str]:
    """在给定的一张价表里查价。命中方式决定 UI 显不显示波浪号。"""
    provider_table = table.get(provider, {})
    model_lower = model.lower().strip()

    for candidate in _variants(model_lower):
        if candidate in provider_table:
            return provider_table[candidate], "exact" if candidate == model_lower else "prefix"

    # 前缀匹配：xxx-v2-20260101 → xxx-v2
    keys = [k for k in provider_table if not k.startswith("__")]
    best = ""
    for key in keys:
        if model_lower.startswith(key) and len(key) > len(best):
            best = key
    if best:
        return provider_table[best], "prefix"

    if "__default__" in provider_table:
        return provider_table["__default__"], "provider_default"

    default = DEFAULT_MODEL_PRICING.get(provider)
    if default:
        return default, "provider_default"

    return None, ""


def _match(provider: str, model: str) -> tuple[dict | None, str]:
    """返回 (价格表项, 命中方式)。命中方式决定 UI 显不显示波浪号。"""
    return _lookup(_merge_pricing(), provider, model)


def builtin_price(provider: str, model: str) -> tuple[dict | None, str]:
    """只查内置表，不含用户自定义。

    ``/api/pricing`` 用它给设置面板一个「回填内置价」的参照 —— 用户想把
    自己改贵的价退回官方价时，得知道官方价是多少，而 ``_match`` 已经被
    自定义价覆盖了，问不出来。
    """
    return _lookup(PRICING_TABLE, provider, model)


@dataclass
class CostEstimate:
    """一次用量估算。``source`` 不是 ``exact`` 时金额前面加 ``~``。"""

    amount: float = 0.0
    currency: str = "CNY"
    source: str = "unknown"   # exact | prefix | provider_default | fallback | free | unknown

    @property
    def is_estimate(self) -> bool:
        return self.source not in ("exact", "free")

    def format(self) -> str:
        if self.source == "free":
            return "免费"
        if self.source == "unknown":
            return ""
        symbol = "¥" if self.currency == "CNY" else "$"
        prefix = "" if self.source == "exact" else "~"
        if self.amount < 0.001:
            return f"{prefix}{symbol}<0.001"
        return f"{prefix}{symbol}{self.amount:.3f}"


def estimate(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> CostEstimate:
    """结构化估算，供需要区分"实价/估算"的宿主使用。

    ``cached_tokens`` 是输入里命中提示缓存的部分，单独按缓存价结算。这个数
    以前被适配器直接丢掉了，于是整段输入按正价算 —— 而缓存价通常只有正价
    的 1/10（Anthropic 读）、1/50（DeepSeek）或 1/2（OpenAI）。
    """
    provider = (provider or "").lower().strip()
    model = (model or "").strip()

    if provider == "ollama":
        return CostEstimate(amount=0.0, source="free")

    prices, source = _match(provider, model)
    if not prices:
        prices = UNKNOWN_PROVIDER_PRICING
        source = "fallback"

    input_cny = prices.get("input_cny", 0)
    output_cny = prices.get("output_cny", 0)
    input_usd = prices.get("input_usd", 0)
    output_usd = prices.get("output_usd", 0)
    cached = max(0, min(int(cached_tokens or 0), int(input_tokens or 0)))
    uncached = max(0, int(input_tokens or 0) - cached)

    if input_cny or output_cny:
        hit_price = prices.get("input_cache_hit_cny", input_cny * _cache_ratio(provider))
        total = (
            uncached / 1000 * input_cny
            + cached / 1000 * hit_price
            + output_tokens / 1000 * output_cny
        )
        return CostEstimate(amount=total, currency="CNY", source=source)

    hit_price_usd = prices.get("input_cache_hit_usd", input_usd * _cache_ratio(provider))
    total = (
        uncached / 1000 * input_usd
        + cached / 1000 * hit_price_usd
        + output_tokens / 1000 * output_usd
    )
    return CostEstimate(amount=total, currency="USD", source=source)


def estimate_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> str:
    """Estimate the cost of a token usage.

    Returns a human-readable string like "¥0.008", "~¥0.145" or "$0.025".
    未知 provider 也会给一个带 ``~`` 的兜底估算，而不是空白。
    """
    try:
        return estimate(
            provider, model, input_tokens, output_tokens, cached_tokens
        ).format()
    except Exception:  # noqa: BLE001 - 费用算不出来不该打断收尾
        logger.debug("[pricing] 估算失败", exc_info=True)
        return ""


#: 缓存命中价相对正价的比例。没有显式配 ``input_cache_hit_*`` 时用它折算。
#: 数值来自各家公开定价：DeepSeek 约 1/50，Anthropic 读约 1/10，OpenAI 约 1/2。
CACHE_HIT_RATIO = {
    "deepseek": 0.02,
    "anthropic": 0.1,
    "openai": 0.5,
}
DEFAULT_CACHE_RATIO = 0.5


def _cache_ratio(provider: str) -> float:
    return CACHE_HIT_RATIO.get(provider, DEFAULT_CACHE_RATIO)


# ── 用户自定义定价的读写（GUI / Web 的设置面板用） ──
def pricing_file_path() -> Path:
    env_path = os.environ.get("GANGGE_PRICING_FILE", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.cwd() / ".gangge" / "pricing.json"


def load_custom_pricing() -> dict:
    """读回用户填的价格（原样，不与内置表合并）。"""
    path = pricing_file_path()
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        logger.debug("[pricing] 读取自定义定价失败", exc_info=True)
        return {}


def save_custom_pricing(data: dict) -> Path:
    """写回自定义定价并让缓存失效。

    结构：``{"provider": {"model": {"input_cny": …, "output_cny": …,
    "input_cache_hit_cny": …}}}``。价格单位是**每千 token**，
    和内置表一致（1M 价 / 1000）。
    """
    path = pricing_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    reset_custom_cache()
    return path


def set_model_price(
    provider: str,
    model: str,
    input_price: float,
    output_price: float,
    cached_price: float | None = None,
) -> Path:
    """便捷写入单个模型的价格（每千 token）。"""
    data = load_custom_pricing()
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    entry: dict[str, float] = {
        "input_cny": float(input_price),
        "output_cny": float(output_price),
    }
    if cached_price is not None:
        entry["input_cache_hit_cny"] = float(cached_price)
    data.setdefault(provider, {})[model] = entry
    return save_custom_pricing(data)


def remove_model_price(provider: str, model: str) -> Path:
    """删掉某个模型的自定义价，回到内置价。

    没有这个入口的话，用户一旦填错就再也退不回内置价了 —— 写盘接口只增不删。
    文件里没有这个模型时什么也不做，直接返回路径。
    """
    data = load_custom_pricing()
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    models = data.get(provider)
    if isinstance(models, dict) and model in models:
        models.pop(model, None)
        if not models:
            data.pop(provider, None)
        return save_custom_pricing(data)
    return pricing_file_path()


def reset_custom_cache() -> None:
    """测试用：忘掉已加载的自定义定价。"""
    global _CUSTOM_CACHE
    _CUSTOM_CACHE = None
