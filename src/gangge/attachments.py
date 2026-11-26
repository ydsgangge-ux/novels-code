"""附件的类型判定，以及"该怎么交给模型"。

判定只写一份。桌面端 ``_add_attachment``、Web 端上传接口、消息拼装都要用，
各写一遍必然出现"一端加了 .docx、另一端忘了"，然后用户以为挂上了，
模型其实什么都没收到。

关键点：Office 文件**不能**走 base64 塞给多模态模型，也不能让模型用
``read_file`` 去读（拿到的是 zip 二进制）。必须引导它用 ``office_read``。
"""

from __future__ import annotations

from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a"}

#: 能被 office_read 直接解析的格式 → 中文名
OFFICE_EXTS = {
    ".docx": "Word 文档",
    ".docm": "Word 文档（含宏）",
    ".xlsx": "Excel 表格",
    ".xlsm": "Excel 表格（含宏）",
    ".pptx": "PPT 演示",
    ".pptm": "PPT 演示（含宏）",
}

PDF_EXTS = {".pdf"}

TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".xml", ".html",
}

#: 97-2003 的二进制格式，没有解析库，只能劝用户另存
LEGACY_EXTS = {
    ".doc": "Word 97-2003",
    ".xls": "Excel 97-2003",
    ".ppt": "PPT 97-2003",
}

ICON = {
    "image": "🖼️",
    "video": "🎬",
    "audio": "🎵",
    "office": "📄",
    "pdf": "📕",
    "text": "📃",
    "legacy": "⚠️",
}

#: 各类附件的读取方式。写在这里是为了两端给模型的提示一字不差。
_READ_HINT = {
    "image": "用 vision 工具识别（传 image_name=文件名 或 image_path=绝对路径）",
    "video": "视频无法直接理解，需要先抽帧再用 vision",
    "audio": "音频无法直接理解，需要先转写",
    "office": "用 office_read(path=...) 读，不要用 read_file（会得到二进制乱码）",
    "pdf": "用 office_read(path=..., pages=\"1-10\") 读文字；若是扫描件（没有文字层），"
           "再用 office_render 渲染成 PNG 交给 vision",
    "text": "用 read_file 直接读",
    "legacy": "旧版二进制格式读不了，请让用户先另存为 .docx / .xlsx / .pptx",
}


def kind_of(path: str | Path) -> str | None:
    """→ image / video / audio / office / pdf / text / legacy；不认识返回 None。"""
    ext = Path(str(path)).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in TEXT_EXTS:
        return "text"
    if ext in LEGACY_EXTS:
        return "legacy"
    return None


def icon_of(path: str | Path) -> str:
    return ICON.get(kind_of(path) or "", "📎")


def label_of(path: str | Path) -> str:
    """给界面看的中文类型名。"""
    ext = Path(str(path)).suffix.lower()
    if ext in OFFICE_EXTS:
        return OFFICE_EXTS[ext]
    if ext in LEGACY_EXTS:
        return LEGACY_EXTS[ext]
    return {
        "image": "图片",
        "video": "视频",
        "audio": "音频",
        "pdf": "PDF",
        "text": "文本",
    }.get(kind_of(path) or "", "文件")


def media_type_of(path: str | Path) -> str:
    """图片/视频/音频的 MIME，供多模态接口使用。"""
    ext = Path(str(path)).suffix.lower()
    special = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".svg": "image/svg+xml",
    }
    if ext in special:
        return special[ext]
    kind = kind_of(path)
    family = {"image": "image", "video": "video", "audio": "audio"}.get(kind or "")
    return f"{family}/{ext.lstrip('.')}" if family else "application/octet-stream"


def read_hint(kind: str | None) -> str:
    return _READ_HINT.get(kind or "", "")


def describe(attachments: list[dict]) -> str:
    """把附件拼成消息末尾的一段说明，告诉模型"这些文件在哪、怎么读"。

    ``attachments`` 每项至少要有 ``name``；有 ``path`` 才算能读。
    """
    if not attachments:
        return ""
    lines = ["", "[附件]"]
    for att in attachments:
        name = str(att.get("name") or "")
        path = str(att.get("path") or "")
        kind = att.get("kind") or kind_of(path or name)
        label = label_of(path or name)
        hint = read_hint(kind)
        if path:
            lines.append(f"- {name}（{label}）：{path}")
        else:
            lines.append(f"- {name}（{label}）")
        if hint:
            lines.append(f"  → {hint}")
    return "\n".join(lines)
