"""Internationalization support for Gangge Code.

Usage:
    from gangge.i18n import t

    label.setText(t("btn_send"))       # "发送" or "Send"
    label.setText(t("status_ready"))   # "就绪" or "Ready"

Language is auto-detected from:
    1. GANGGE_LANG environment variable
    2. System locale (LANG / LC_ALL on Unix, system default on Windows)
    3. Falls back to "zh" (Chinese)

To add a new language:
    1. Create a JSON file in gangge/i18n/ (e.g. ja.json)
    2. Add all keys from zh.json with translated values
    3. Register it in _LANG_FILES below
"""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path
from typing import Any

_LANG_FILES = {
    "zh": "zh.json",
    "en": "en.json",
}

_data: dict[str, dict[str, str]] = {}
_current_lang: str = "zh"


def _detect_language() -> str:
    env_lang = os.environ.get("GANGGE_LANG", "").strip().lower()
    if env_lang:
        prefix = env_lang[:2]
        if prefix in _LANG_FILES:
            return prefix

    try:
        sys_loc = locale.getlocale()[0] or ""
    except Exception:
        try:
            sys_loc = locale.getdefaultlocale()[0] or ""
        except Exception:
            sys_loc = ""
    if sys_loc.lower().startswith("zh"):
        return "zh"
    if sys_loc.lower().startswith("en"):
        return "en"

    for env_var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        val = os.environ.get(env_var, "").lower()
        if val.startswith("zh"):
            return "zh"
        if val.startswith("en"):
            return "en"

    return "zh"


def _load_lang(lang: str) -> dict[str, str]:
    if lang in _data:
        return _data[lang]

    json_path = Path(__file__).parent / _LANG_FILES.get(lang, "zh.json")
    try:
        with open(json_path, encoding="utf-8") as f:
            _data[lang] = json.load(f)
    except Exception:
        _data[lang] = {}
    return _data[lang]


def set_language(lang: str) -> None:
    global _current_lang
    if lang in _LANG_FILES:
        _current_lang = lang
        _load_lang(lang)


def get_language() -> str:
    return _current_lang


def t(key: str, **kwargs: Any) -> str:
    """Translate a key to the current language.

    Supports format placeholders: t("msg_rounds", n=3) → "第 3 轮" / "Round 3"
    """
    lang_data = _load_lang(_current_lang)
    text = lang_data.get(key)
    if text is None:
        fallback = _load_lang("zh")
        text = fallback.get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


_current_lang = _detect_language()
_load_lang(_current_lang)
