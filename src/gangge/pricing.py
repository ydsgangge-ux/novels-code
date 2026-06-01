"""Token pricing estimation for supported LLM providers.

Pricing source: https://api-docs.deepseek.com/quick_start/pricing/
DeepSeek 2026年4月更新：官方已按 1M tokens 计费。
此处换算为 每千token 价格以便计算。

注意：实际费用比计算值通常更低，因为：
- DeepSeek 缓存命中价仅 $0.0028/1M（比正价便宜 50 倍）
- 系统提示和工具定义等重复内容大部分命中缓存
"""

from __future__ import annotations

# Per-1M-token pricing from official sources, converted to per-1K for calculation
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
        "gpt-4o": {"input_usd": 0.0025, "output_usd": 0.01},
        "gpt-4o-mini": {"input_usd": 0.00015, "output_usd": 0.0006},
        "gpt-4-turbo": {"input_usd": 0.01, "output_usd": 0.03},
        "gpt-3.5-turbo": {"input_usd": 0.0005, "output_usd": 0.0015},
    },
    "anthropic": {
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
    },
    "zhipu": {
        "__default__": {"input_cny": 0.001, "output_cny": 0.001},
        "glm-4-plus": {"input_cny": 0.05, "output_cny": 0.05},
        "glm-4": {"input_cny": 0.1, "output_cny": 0.1},
        "glm-4-air": {"input_cny": 0.001, "output_cny": 0.001},
        "glm-4-flash": {"input_cny": 0.0001, "output_cny": 0.0001},
    },
    "moonshot": {
        "__default__": {"input_cny": 0.012, "output_cny": 0.012},
        "moonshot-v1-8k": {"input_cny": 0.012, "output_cny": 0.012},
        "moonshot-v1-32k": {"input_cny": 0.024, "output_cny": 0.024},
        "moonshot-v1-128k": {"input_cny": 0.06, "output_cny": 0.06},
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
    "custom": {},
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


def estimate_cost(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> str:
    """Estimate the cost of a token usage.

    Returns a human-readable string like "¥0.008" or "$0.025".
    """
    provider = provider.lower()
    provider_prices = PRICING_TABLE.get(provider, {})
    model_prices = provider_prices.get(model) or provider_prices.get("__default__")

    if not model_prices:
        model_prices = DEFAULT_MODEL_PRICING.get(provider)

    if not model_prices:
        return ""

    input_cny = model_prices.get("input_cny", 0)
    input_usd = model_prices.get("input_usd", 0)
    output_cny = model_prices.get("output_cny", 0)
    output_usd = model_prices.get("output_usd", 0)

    if provider == "ollama":
        return "免费"

    # Use CNY for Chinese providers, USD otherwise
    if input_cny or output_cny:
        total = (input_tokens / 1000 * input_cny) + (output_tokens / 1000 * output_cny)
        if total < 0.001:
            return f"¥<0.001"
        return f"¥{total:.3f}"
    else:
        total = (input_tokens / 1000 * input_usd) + (output_tokens / 1000 * output_usd)
        if total < 0.001:
            return f"$<0.001"
        return f"${total:.3f}"
