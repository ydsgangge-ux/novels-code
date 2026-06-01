"""Repository Index — multi-language symbol scanning, import graph, and caching.

Scans project files to build a structured index:
  - symbols: classes, functions, methods with line numbers
  - imports: per-file import/require statements
  - exports: per-file exported symbols
  - dependencies: inter-file dependency graph

The index is cached in .gangge/repo_index.json and incrementally updated.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

INDEX_VERSION = 2
EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".idea", ".vscode", ".egg-info", ".gangge",
    "env", ".tox", ".mypy_cache", ".pytest_cache", "gangge_projects",
}

LANG_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "jsx",
    ".tsx": "tsx",
    ".html": "html",
    ".css": "css",
    ".json": "json",
    ".md": "markdown",
}


def _should_skip(path: Path) -> bool:
    return any(p in EXCLUDE_DIRS for p in path.parts)


def scan_python(text: str) -> dict[str, Any]:
    """Parse Python file using ast module."""
    symbols = []
    imports = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith("class ") and ":" in s:
                name = s.split("(")[0].replace("class ", "").replace(":", "").strip()
                symbols.append({"name": name, "kind": "class", "line": i})
            elif s.startswith(("def ", "async def ")):
                name = s.replace("async def ", "").replace("def ", "").split("(")[0].strip()
                symbols.append({"name": name, "kind": "function", "line": i})
            elif s.startswith(("import ", "from ")):
                imports.append(s.rstrip(";"))
        return {"symbols": symbols, "imports": imports}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.append({"name": node.name, "kind": "class", "line": node.lineno})
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({
                        "name": f"{node.name}.{item.name}",
                        "kind": "method",
                        "line": item.lineno,
                    })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not getattr(node, "_in_class", False):
                symbols.append({"name": node.name, "kind": "function", "line": node.lineno})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {module} import {names}")

    return {"symbols": symbols, "imports": imports}


def scan_javascript(text: str) -> dict[str, Any]:
    """Parse JavaScript/TypeScript file using regex."""
    symbols = []
    imports = []
    exports = []

    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()

        if s.startswith("import ") and "from" in s:
            imports.append(s.rstrip(";"))
        elif s.startswith("require("):
            imports.append(s.rstrip(";"))

        if "export " in s:
            exports.append(s.rstrip(";"))
            if "function " in s or "const " in s or "class " in s:
                m = re.search(
                    r"(?:export\s+(?:default\s+)?)"
                    r"(?:function\s+(\w+)|const\s+(\w+)|class\s+(\w+))",
                    s,
                )
                if m:
                    name = m.group(1) or m.group(2) or m.group(3)
                    kind = "class" if "class " in s else "function"
                    symbols.append({"name": name, "kind": kind, "line": i})
        elif re.match(r"(?:async\s+)?function\s+(\w+)", s):
            m = re.match(r"(?:async\s+)?function\s+(\w+)", s)
            if m:
                symbols.append({"name": m.group(1), "kind": "function", "line": i})
        elif re.match(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\()", s):
            m = re.match(r"(?:const|let|var)\s+(\w+)", s)
            if m:
                symbols.append({"name": m.group(1), "kind": "function", "line": i})
        elif re.match(r"class\s+(\w+)", s):
            m = re.match(r"class\s+(\w+)", s)
            if m:
                symbols.append({"name": m.group(1), "kind": "class", "line": i})

    return {"symbols": symbols, "imports": imports, "exports": exports}


def scan_html(text: str) -> dict[str, Any]:
    """Parse HTML file for script/link references."""
    imports = []
    for m in re.finditer(r'<script\s+[^>]*src=["\']([^"\']+)["\']', text):
        imports.append(f'script: {m.group(1)}')
    for m in re.finditer(r'<link\s+[^>]*href=["\']([^"\']+\.css)["\']', text):
        imports.append(f'stylesheet: {m.group(1)}')
    return {"symbols": [], "imports": imports}


def scan_file(filepath: Path, workspace: Path) -> dict[str, Any] | None:
    """Scan a single file and return its index entry."""
    ext = filepath.suffix.lower()
    lang = LANG_EXTENSIONS.get(ext)
    if not lang:
        return None

    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    if len(text) > 500_000:
        return None

    rel = str(filepath.relative_to(workspace)).replace("\\", "/")

    if lang == "python":
        result = scan_python(text)
    elif lang in ("javascript", "typescript", "jsx", "tsx"):
        result = scan_javascript(text)
    elif lang == "html":
        result = scan_html(text)
    else:
        result = {"symbols": [], "imports": []}

    result["path"] = rel
    result["lang"] = lang
    result["lines"] = text.count("\n") + 1
    try:
        result["mtime"] = filepath.stat().st_mtime
    except OSError:
        pass
    return result


def build_index(workspace: str) -> dict[str, Any]:
    """Build full repository index for a workspace."""
    ws = Path(workspace)
    if not ws.is_dir():
        return {"version": INDEX_VERSION, "files": {}, "timestamp": ""}

    files = {}
    for filepath in sorted(ws.rglob("*")):
        if not filepath.is_file():
            continue
        if _should_skip(filepath):
            continue
        entry = scan_file(filepath, ws)
        if entry:
            files[entry["path"]] = entry

    return {
        "version": INDEX_VERSION,
        "files": files,
        "timestamp": datetime.now().isoformat(),
    }


def build_dependency_graph(index: dict[str, Any], workspace: str) -> dict[str, list[str]]:
    """Build inter-file dependency graph from import statements."""
    ws = Path(workspace)
    files = index.get("files", {})
    all_paths = set(files.keys())
    graph: dict[str, list[str]] = {}

    for path, entry in files.items():
        deps = set()
        imports = entry.get("imports", [])
        lang = entry.get("lang", "")

        for imp in imports:
            resolved = _resolve_import(imp, lang, path, all_paths, ws)
            if resolved and resolved != path:
                deps.add(resolved)

        if deps:
            graph[path] = sorted(deps)

    return graph


def _resolve_import(
    imp: str, lang: str, current_path: str, all_paths: set[str], ws: Path
) -> str | None:
    """Try to resolve an import statement to a file path in the project."""
    if lang == "python":
        m = re.match(r"from\s+([\w.]+)\s+import", imp) or re.match(r"import\s+([\w.]+)", imp)
        if m:
            mod = m.group(1).replace(".", "/")
            candidates = [f"{mod}.py", f"{mod}/__init__.py"]
            for c in candidates:
                if c in all_paths:
                    return c
            parts = mod.split("/")
            for i in range(len(parts), 0, -1):
                partial = "/".join(parts[:i])
                for c in [f"{partial}.py", f"{partial}/__init__.py"]:
                    if c in all_paths:
                        return c

    elif lang in ("javascript", "typescript", "jsx", "tsx"):
        m = re.search(r"""from\s+['"]([^'"]+)['"]""", imp) or re.search(r"""require\(['"]([^'"]+)['"]\)""", imp)
        if m:
            mod = m.group(1)
            if mod.startswith("."):
                cur_dir = str(Path(current_path).parent)
                resolved = str(Path(cur_dir) / mod).replace("\\", "/")
                for ext in ["", ".js", ".ts", ".jsx", ".tsx", "/index.js", "/index.ts"]:
                    candidate = resolved + ext
                    if candidate in all_paths:
                        return candidate

    return None


def save_index(index: dict[str, Any], workspace: str) -> str:
    """Save index to .gangge/repo_index.json."""
    gangge_dir = Path(workspace) / ".gangge"
    gangge_dir.mkdir(parents=True, exist_ok=True)
    index_file = gangge_dir / "repo_index.json"
    try:
        index_file.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(index_file)
    except Exception as e:
        logger.warning(f"Failed to save repo index: {e}")
        return ""


def load_index(workspace: str) -> dict[str, Any] | None:
    """Load cached index from .gangge/repo_index.json."""
    index_file = Path(workspace) / ".gangge" / "repo_index.json"
    if not index_file.exists():
        return None
    try:
        data = json.loads(index_file.read_text(encoding="utf-8"))
        if data.get("version") == INDEX_VERSION:
            return data
    except Exception:
        pass
    return None


def incremental_update(index: dict[str, Any], workspace: str) -> dict[str, Any]:
    """Incrementally update index: only rescan changed files."""
    ws = Path(workspace)
    files = index.get("files", {})

    current_files = set()
    for filepath in sorted(ws.rglob("*")):
        if not filepath.is_file():
            continue
        if _should_skip(filepath):
            continue
        ext = filepath.suffix.lower()
        if ext not in LANG_EXTENSIONS:
            continue
        rel = str(filepath.relative_to(ws)).replace("\\", "/")
        current_files.add(rel)

        try:
            mtime = filepath.stat().st_mtime
        except OSError:
            continue

        existing = files.get(rel)
        if existing and existing.get("mtime") == mtime:
            continue

        entry = scan_file(filepath, ws)
        if entry:
            entry["mtime"] = mtime
            files[rel] = entry

    for old_path in list(files.keys()):
        if old_path not in current_files:
            del files[old_path]

    index["files"] = files
    index["timestamp"] = datetime.now().isoformat()
    return index


def format_project_map(index: dict[str, Any], dep_graph: dict[str, list[str]] | None = None, max_entries: int = 120) -> str:
    """Format the index into a compact project map for the system prompt."""
    files = index.get("files", {})
    if not files:
        return ""

    lines = []
    for path in sorted(files.keys()):
        entry = files[path]
        lang = entry.get("lang", "?")
        syms = entry.get("symbols", [])
        imps = entry.get("imports", [])
        line_count = entry.get("lines", 0)

        sym_str = ""
        if syms:
            parts = []
            for s in syms[:12]:
                kind_icon = {"class": "C", "function": "F", "method": "M"}.get(s["kind"], "?")
                parts.append(f"{kind_icon}:{s['name']}")
            sym_str = " ".join(parts)
            if len(syms) > 12:
                sym_str += f" +{len(syms)-12}"

        imp_count = len(imps)
        dep_str = ""
        if dep_graph and path in dep_graph:
            dep_str = f" ←{len(dep_graph[path])}"

        line_info = f"L{line_count}" if line_count else ""
        parts = [f"- `{path}` [{lang}]"]
        if line_info:
            parts.append(f"{line_info}")
        if sym_str:
            parts.append(sym_str)
        if imp_count:
            parts.append(f"imports:{imp_count}")
        if dep_str:
            parts.append(dep_str)

        lines.append(" ".join(parts))

    if len(lines) > max_entries:
        truncated = lines[:max_entries]
        truncated.append(f"... 共 {len(lines)} 个文件，仅显示前 {max_entries} 个")
        return "\n".join(truncated)

    return "\n".join(lines)


def format_symbol_table(index: dict[str, Any]) -> str:
    """Format a symbol lookup table for the system prompt."""
    files = index.get("files", {})
    if not files:
        return ""

    lines = ["## Symbol 索引"]
    for path in sorted(files.keys()):
        syms = files[path].get("symbols", [])
        if not syms:
            continue
        for s in syms[:8]:
            kind = s["kind"]
            name = s["name"]
            line = s["line"]
            lines.append(f"- {name} ({kind}) → `{path}:{line}`")

    return "\n".join(lines[:200])


def get_or_build_index(workspace: str) -> dict[str, Any]:
    """Get cached index or build a new one."""
    index = load_index(workspace)
    if index:
        index = incremental_update(index, workspace)
    else:
        index = build_index(workspace)
    save_index(index, workspace)
    return index
