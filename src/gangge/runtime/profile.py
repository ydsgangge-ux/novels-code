"""Profiles and bundles — named assemblies of the plugin tree.

A **profile** is a named assembly stored in the harness home. It lists the
bundles it stacks, holds its own ``cordis.patch.yml``, and carries the user's
overrides. A **bundle** is a distributable patch document: the Cordis
configuration rows for one slice of capability, always patchable by the layers
above it.

Layer order, later wins::

    each bundle in profile order  →  profile cordis.patch.yml
                                  →  home cordis.patch.yml
                                  →  --patch overlays

Layout on disk (all optional except the builtins)::

    <workspace>/.gangge/profiles/<name>.yml     profile definition
    <workspace>/.gangge/profiles/<name>.patch.yml   profile-local patch
    <workspace>/.gangge/bundles/<bundle>.yml    bundle override
    <workspace>/.gangge/cordis.patch.yml        home patch
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from gangge.runtime.patch import PatchError, Tree, build_tree, load_document, parse_ops

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent
BUILTIN_PROFILES = BUILTIN_DIR / "profiles"
BUILTIN_BUNDLES = BUILTIN_DIR / "bundles"

HOME_PATCH_NAME = "cordis.patch.yml"


class ProfileError(RuntimeError):
    """A profile or one of its bundles cannot be resolved."""


@dataclass
class Profile:
    """A named assembly of bundles plus its own patch overlay."""

    name: str
    bundles: list[str] = field(default_factory=list)
    description: str = ""
    patch_path: Path | None = None
    source: Path | None = None


@dataclass
class Resolved:
    """Everything needed to boot, plus provenance for ``--dump-config``."""

    profile: Profile
    tree: Tree
    layers: list[str] = field(default_factory=list)
    workspace: Path | None = None


# ── Search paths ────────────────────────────────────────────────


def _candidate_dirs(workspace: Path | None) -> list[Path]:
    """User-owned locations, most specific first."""
    dirs: list[Path] = []
    if workspace is not None:
        dirs.append(Path(workspace) / ".gangge")
    dirs.append(Path.home() / ".gangge")
    return dirs


def _find(kind: str, name: str, workspace: Path | None, suffix: str = ".yml") -> Path | None:
    """Look for ``<dir>/<kind>/<name><suffix>`` in user dirs, optionally builtin."""
    for base in _candidate_dirs(workspace):
        path = base / kind / f"{name}{suffix}"
        if path.exists():
            return path
    for ext in (".yml", ".yaml"):
        builtin = (BUILTIN_PROFILES if kind == "profiles" else BUILTIN_BUNDLES) / f"{name}{ext}"
        if builtin.exists():
            return builtin
    return None


# ── Loading ─────────────────────────────────────────────────────


def load_profile(name: str, workspace: Path | None = None) -> Profile:
    """Resolve a profile by name from user dirs, falling back to builtins."""
    path = _find("profiles", name, workspace)
    if path is None:
        available = ", ".join(available_profiles(workspace)) or "(none)"
        raise ProfileError(f"unknown profile '{name}'. Available: {available}")

    doc = _read_mapping(path)
    bundles = doc.get("bundles") or []
    if not isinstance(bundles, list) or not all(isinstance(b, str) for b in bundles):
        raise ProfileError(f"{path.name}: 'bundles' must be a list of bundle names")

    patch_name = doc.get("patch")
    patch_path: Path | None = None
    if patch_name:
        candidate = (path.parent / str(patch_name)).resolve()
        patch_path = candidate if candidate.exists() else None
        if patch_path is None:
            logger.warning("%s: declared patch '%s' not found", path.name, patch_name)
    else:
        default = path.with_suffix(".patch.yml")
        if default.exists():
            patch_path = default

    return Profile(
        name=str(doc.get("name") or name),
        bundles=list(bundles),
        description=str(doc.get("description") or ""),
        patch_path=patch_path,
        source=path,
    )


def _read_mapping(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in (".yml", ".yaml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                f"PyYAML is required to read {path.name}. Install it with `pip install pyyaml`."
            ) from exc
        data = yaml.safe_load(text) or {}
    elif suffix == ".json":
        import json

        data = json.loads(text or "{}")
    else:
        raise ProfileError(f"unsupported profile format '{suffix}'")
    if not isinstance(data, dict):
        raise ProfileError(f"{path.name}: a profile must be a mapping")
    return data


def available_profiles(workspace: Path | None = None) -> list[str]:
    """Every profile name visible from ``workspace``, deduplicated."""
    seen: dict[str, None] = {}
    for base in _candidate_dirs(workspace):
        for p in sorted((base / "profiles").glob("*.y*ml")):
            if p.name.endswith(".patch.yml"):
                continue
            seen.setdefault(p.stem, None)
    for p in sorted(BUILTIN_PROFILES.glob("*.y*ml")):
        if p.name.endswith(".patch.yml"):
            continue
        seen.setdefault(p.stem, None)
    return list(seen)


def bundle_path(name: str, workspace: Path | None = None) -> Path:
    """Locate one bundle's patch document."""
    for base in _candidate_dirs(workspace):
        for ext in (".yml", ".yaml"):
            path = base / "bundles" / f"{name}{ext}"
            if path.exists():
                return path
    for ext in (".yml", ".yaml"):
        path = BUILTIN_BUNDLES / f"{name}{ext}"
        if path.exists():
            return path
    raise ProfileError(f"unknown bundle '{name}'. Expected {BUILTIN_BUNDLES / (name + '.yml')}")


def profile_patches() -> list[str]:
    """Overlay files passed through the environment by the CLI.

    ``gangge --patch a.yml --patch b.yml`` exports ``GANGGE_PATCH`` so the UI
    layers — which never see argv — pick the same overlays up.
    """
    raw = os.environ.get("GANGGE_PATCH", "").strip()
    if not raw:
        return []
    return [part for part in raw.split(os.pathsep) if part]


# ── Resolution ──────────────────────────────────────────────────


def resolve(
    name: str = "standard",
    workspace: str | Path | None = None,
    patches: Sequence[str | Path] = (),
    include_home_patch: bool = True,
) -> Resolved:
    """Compose the plugin tree for ``name`` with every overlay applied."""
    ws = Path(workspace).resolve() if workspace else None
    profile = load_profile(name, ws)

    layers: list[tuple[str, Any]] = []
    for bundle in profile.bundles:
        path = bundle_path(bundle, ws)
        try:
            ops = parse_ops(load_document(path), source=f"bundle:{bundle}")
        except PatchError as exc:
            raise ProfileError(str(exc)) from exc
        layers.append((f"bundle:{bundle}", ops))

    if profile.patch_path is not None:
        layers.append((f"profile:{profile.name}", parse_ops(
            load_document(profile.patch_path), source=f"profile:{profile.name}")))

    if include_home_patch and ws is not None:
        home_patch = ws / ".gangge" / HOME_PATCH_NAME
        if home_patch.exists():
            layers.append(("home", parse_ops(load_document(home_patch), source="home")))

    for raw in patches:
        path = Path(raw)
        layers.append((f"overlay:{path.name}", parse_ops(
            load_document(path), source=f"overlay:{path.name}")))

    tree = build_tree(layers)
    if not len(tree):
        raise ProfileError(f"profile '{name}' resolved to an empty plugin tree")

    return Resolved(
        profile=profile,
        tree=tree,
        layers=[source for source, _ in layers],
        workspace=ws,
    )
