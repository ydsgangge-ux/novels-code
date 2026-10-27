"""LLM registry — factory for creating LLM instances from config."""

from __future__ import annotations

import os
from pathlib import Path

from gangge.layer5_llm.base import BaseLLM
from gangge.layer5_llm.anthropic import AnthropicLLM
from gangge.layer5_llm.openai_compat import OpenAICompatLLM


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _load_env() -> None:
    """Load .env file if present."""
    try:
        from dotenv import load_dotenv

        # Search upward from cwd for .env
        cwd = Path.cwd()
        for p in [cwd, *cwd.parents]:
            if (p / ".env").exists():
                load_dotenv(p / ".env")
                break
    except ImportError:
        pass


def create_llm(provider: str | None = None) -> BaseLLM:
    """Create an LLM instance based on configuration.

    Reads from environment variables (or .env file).
    Provider priority: explicit arg > LLM_PROVIDER env > default (anthropic).
    """
    _load_env()

    provider = (provider or _env("LLM_PROVIDER") or "anthropic").lower()
    max_tokens = int(_env("MAX_TOKENS", "8192"))
    temperature = float(_env("TEMPERATURE", "0.0"))

    if provider == "anthropic":
        api_key = _env("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        model = _env("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        return AnthropicLLM(
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    elif provider == "openai":
        api_key = _env("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        model = _env("OPENAI_MODEL", "gpt-4o")
        return OpenAICompatLLM(
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    elif provider == "deepseek":
        api_key = _env("DEEPSEEK_API_KEY", "")
        base_url = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        model = _env("DEEPSEEK_MODEL", "deepseek-chat")
        return OpenAICompatLLM(
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    elif provider == "ollama":
        base_url = _env("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        model = _env("OLLAMA_MODEL", "llama3.1")
        return OpenAICompatLLM(
            base_url=base_url,
            api_key="ollama",
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    elif provider == "qwen":
        api_key = _env("QWEN_API_KEY", "")
        base_url = _env("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        model = _env("QWEN_MODEL", "qwen-max")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "zhipu":
        api_key = _env("ZHIPU_API_KEY", "")
        base_url = _env("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        model = _env("ZHIPU_MODEL", "glm-4-plus")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "moonshot":
        api_key = _env("MOONSHOT_API_KEY", "")
        base_url = _env("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
        model = _env("MOONSHOT_MODEL", "moonshot-v1-auto")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "baichuan":
        api_key = _env("BAICHUAN_API_KEY", "")
        base_url = _env("BAICHUAN_BASE_URL", "https://api.baichuan-ai.com/v1")
        model = _env("BAICHUAN_MODEL", "Baichuan4")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "yi":
        api_key = _env("YI_API_KEY", "")
        base_url = _env("YI_BASE_URL", "https://api.lingyiwanwu.com/v1")
        model = _env("YI_MODEL", "yi-large")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "minimax":
        api_key = _env("MINIMAX_API_KEY", "")
        base_url = _env("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
        model = _env("MINIMAX_MODEL", "MiniMax-Text-01")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "stepfun":
        api_key = _env("STEPFUN_API_KEY", "")
        base_url = _env("STEPFUN_BASE_URL", "https://api.stepfun.com/v1")
        model = _env("STEPFUN_MODEL", "step-2-16k")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "siliconflow":
        api_key = _env("SILICONFLOW_API_KEY", "")
        base_url = _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        model = _env("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V3")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "mimo":
        api_key = _env("MIMO_API_KEY", "")
        base_url = _env("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
        model = _env("MIMO_MODEL", "mimo-v2.5-pro")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    elif provider == "custom":
        api_key = _env("CUSTOM_API_KEY", "not-needed")
        base_url = _env("CUSTOM_BASE_URL", "")
        model = _env("CUSTOM_MODEL", "")
        if not base_url or not model:
            raise ValueError("CUSTOM_BASE_URL and CUSTOM_MODEL must be set for custom provider")
        return OpenAICompatLLM(
            base_url=base_url, api_key=api_key, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    else:
        raise ValueError(
            f"Unknown LLM provider: {provider}. "
            f"Supported: anthropic, openai, deepseek, qwen, zhipu, moonshot, "
            f"baichuan, yi, minimax, stepfun, siliconflow, mimo, ollama, custom"
        )


# Default endpoints for the vision route. Non-Anthropic entries all speak the
# OpenAI chat-completions dialect, so one adapter covers them.
_VISION_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "moonshot": "https://api.moonshot.cn/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "stepfun": "https://api.stepfun.com/v1",
    "yi": "https://api.lingyiwanwu.com/v1",
    "minimax": "https://api.minimax.chat/v1",
    "baichuan": "https://api.baichuan-ai.com/v1",
    "mimo": "https://api.xiaomimimo.com/v1",
    "ollama": "http://localhost:11434/v1",
    "custom": "",
}


def create_vision_llm() -> BaseLLM | None:
    """Build a dedicated vision adapter from ``VISION_*`` environment variables.

    Returns ``None`` when nothing vision-specific is configured — the caller
    then falls back to the main model, which is only valid if that model
    declares ``supports_vision``.

    ``VISION_MODEL`` alone is enough: provider, base URL and API key are
    inherited from the main configuration, so pointing an existing
    SiliconFlow / Qwen / Zhipu setup at a VL model is one line in ``.env``.
    Set ``VISION_PROVIDER`` only when the vision model lives elsewhere.
    """
    _load_env()

    model = _env("VISION_MODEL").strip()
    provider = _env("VISION_PROVIDER").strip().lower()
    if not model and not provider:
        return None
    if not model:
        raise ValueError("VISION_MODEL is not set (required when VISION_PROVIDER is set)")

    provider = provider or (_env("LLM_PROVIDER") or "anthropic").lower()
    max_tokens = int(_env("VISION_MAX_TOKENS") or _env("MAX_TOKENS", "4096"))
    temperature = float(_env("VISION_TEMPERATURE") or _env("TEMPERATURE", "0.0"))

    if provider == "anthropic":
        api_key = _env("VISION_API_KEY") or _env("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        return AnthropicLLM(
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    if provider not in _VISION_BASE_URLS:
        raise ValueError(
            f"Unknown VISION_PROVIDER: {provider}. Supported: anthropic, "
            + ", ".join(sorted(_VISION_BASE_URLS))
        )

    upper = provider.upper()
    base_url = (
        _env("VISION_BASE_URL")
        or _env(f"{upper}_BASE_URL")
        or _VISION_BASE_URLS[provider]
    )
    api_key = (
        _env("VISION_API_KEY")
        or _env(f"{upper}_API_KEY")
        or ("ollama" if provider == "ollama" else "not-needed")
    )
    if not base_url:
        raise ValueError(f"VISION_BASE_URL is not set for provider {provider}")

    return OpenAICompatLLM(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        supports_vision=True,
    )
