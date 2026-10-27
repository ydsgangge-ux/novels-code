"""Boot — turn a resolved profile into a live plugin tree.

``bootstrap()`` is the only place that mounts anything. Everything a caller
touches afterwards is reached through ``ctx.<key>``, so swapping an
implementation is a profile edit rather than an import edit.

A plugin that fails to mount is recorded in ``Runtime.failed`` and reported —
it never aborts the boot. The reasoning is the same as in DeepSeek Harness: one
broken optional capability must not take the whole runtime down, but a missing
*core* service must be loud, which is what ``Runtime.missing()`` is for.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from gangge.runtime import seams
from gangge.runtime.context import Context
from gangge.runtime.events import catalogue
from gangge.runtime.patch import Row
from gangge.runtime.profile import ProfileError, Resolved, available_profiles, resolve

logger = logging.getLogger(__name__)


class BootError(RuntimeError):
    """The runtime could not be assembled."""


@dataclass
class Runtime:
    """A booted plugin tree plus the provenance needed to explain it."""

    ctx: Context
    profile: str
    resolved: Resolved
    rows: list[Row] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    # -- service access ---------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self.ctx.get(key, default)

    def require(self, key: str) -> Any:
        return self.ctx.require(key)

    @property
    def llm(self) -> Any:
        return self.ctx.get(seams.LLM)

    @property
    def tools(self) -> Any:
        return self.ctx.get(seams.TOOLS)

    @property
    def permission(self) -> Any:
        return self.ctx.get(seams.PERMISSION)

    @property
    def sessions(self) -> Any:
        return self.ctx.get(seams.SESSIONS)

    @property
    def telemetry(self) -> Any:
        return self.ctx.get(seams.TELEMETRY)

    @property
    def memory(self) -> Any:
        return self.ctx.get(seams.MEMORY)

    # -- health -----------------------------------------------

    def missing(self) -> list[str]:
        """Core services that no mounted plugin provided."""
        return [key for key in seams.CORE_SERVICES if not self.ctx.has(key)]

    def describe(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "layers": self.resolved.layers,
            "rows": len(self.rows),
            "failed": [{"row": r, "error": e} for r, e in self.failed],
            "missing": self.missing(),
            "context": self.ctx.describe(),
        }

    def dump_config(self) -> str:
        """Human/machine readable view of the composed tree."""
        return dump_tree(self.resolved, live=self)

    # -- teardown ---------------------------------------------

    def dispose(self) -> None:
        self.ctx.dispose()

    async def aclose(self) -> None:
        await self.ctx.aclose()


# ── Boot ────────────────────────────────────────────────────────


def bootstrap(
    profile: str = "standard",
    workspace: str | Path | None = None,
    patches: Sequence[str | Path] = (),
    overrides: dict[str, dict[str, Any]] | None = None,
    include_home_patch: bool = True,
    extra_config: dict[str, Any] | None = None,
    bindings: dict[str, dict[str, Any]] | None = None,
) -> Runtime:
    """Compose ``profile`` and mount every enabled row.

    ``overrides`` is a last-writer convenience for programmatic callers (and
    ``--set id.key=value`` on the CLI) — it is applied as a final patch layer,
    so nothing bypasses the documented layering.

    ``bindings`` carries host-provided runtime values that cannot live in YAML
    (callbacks, already-open handles). They are merged into a row's config at
    mount time and never written back to the profile, so a YAML file remains the
    single description of the tree.
    """
    try:
        resolved = resolve(
            profile,
            workspace=workspace,
            patches=patches,
            include_home_patch=include_home_patch,
        )
    except ProfileError as exc:
        raise BootError(str(exc)) from exc

    if overrides:
        resolved.tree.patch(
            [{"id": k, "config": v} for k, v in overrides.items()],
            source="override",
        )
        resolved.layers.append("override")

    ctx = Context(name=f"gangge:{profile}")
    ctx.data["workspace"] = str(Path(workspace).resolve()) if workspace else str(Path.cwd())
    ctx.data["profile"] = profile
    if extra_config:
        ctx.data.update(extra_config)

    runtime = Runtime(ctx=ctx, profile=profile, resolved=resolved, rows=resolved.tree.rows)

    for row in resolved.tree.enabled_rows():
        _mount_row(runtime, row, bindings or {})

    # Second pass: rows parked on dependencies may have woken up during the
    # first pass' provides, but a row inserted after them could still have left
    # one parked. Report anything still waiting so it is a visible warning.
    for row in resolved.tree.enabled_rows():
        mount = row.mounted
        if mount is not None and not mount.active:
            inject = getattr(mount.plugin, "inject", None)
            required = getattr(inject, "required", ()) or ()
            waiting = [k for k in required if not ctx.has(k)]
            runtime.failed.append((row.id, f"parked on missing service(s): {', '.join(waiting)}"))
            logger.warning("[boot] row '%s' never activated — waiting for %s", row.id, waiting)

    _validate_seams(ctx)

    missing = runtime.missing()
    if missing:
        logger.warning("[boot] profile '%s' is missing core services: %s", profile, missing)

    logger.info(
        "[boot] profile=%s rows=%d active=%d stopped=%d",
        profile,
        len(runtime.rows),
        sum(1 for r in runtime.rows if r.mounted is not None and r.mounted.active),
        len(runtime.failed),
    )
    return runtime


def _validate_seams(ctx: Context) -> None:
    """Warn when a mounted implementation is missing declared seam members.

    Only a warning: a third-party plugin may legitimately duck-type a subset,
    and taking the runtime down over an interface nicety would be worse than
    the missing method itself.
    """
    for spec in seams.SEAMS:
        value = ctx.get(spec.key)
        if value is None:
            continue
        absent = seams.check_implementation(spec.key, value)
        if absent:
            logger.warning(
                "[boot] '%s' does not implement %s fully — missing: %s",
                spec.key,
                spec.interface,
                ", ".join(absent),
            )


def _mount_row(runtime: Runtime, row: Row, bindings: dict[str, dict[str, Any]]) -> None:
    try:
        plugin = importlib.import_module(row.name)
    except Exception as exc:
        logger.error("[boot] cannot import '%s' for row '%s': %s", row.name, row.id, exc)
        runtime.failed.append((row.id, f"import failed: {type(exc).__name__}: {exc}"))
        return

    config = dict(row.config)
    config.update(bindings.get(row.id, {}))
    config.setdefault("workspace", runtime.ctx.data.get("workspace", ""))
    mount = runtime.ctx.plugin(plugin, config)
    row.mounted = mount
    if not mount.active:
        logger.debug("[boot] row '%s' parked", row.id)


# ── Introspection helpers (used by the CLI) ────────────────────


def dump_tree(resolved: Resolved, live: Runtime | None = None) -> str:
    """Render the composed tree. Pass ``live`` to also show provided services.

    Without ``live`` this is a *static* dump: it imports nothing and mounts
    nothing, so ``gangge --dump-config`` is safe to run on a machine with no
    API key and no optional dependencies installed.
    """
    lines: list[str] = [f"# gangge runtime — profile: {resolved.profile.name}"]
    if resolved.profile.description:
        lines.append(f"# {resolved.profile.description}")
    if resolved.profile.source:
        lines.append(f"# definition: {resolved.profile.source}")
    lines.append("# layers (applied in order, later wins):")
    for layer in resolved.layers:
        lines.append(f"#   - {layer}")
    lines.append("")
    lines.append("rows:")

    for row in resolved.tree.rows:
        state = "on " if resolved.tree.is_enabled(row) else "off"
        source = f"  <- {row.source}" if row.source else ""
        lines.append(f"  [{state}] {row.id:<16} {row.name}{source}")
        for key, value in (row.config or {}).items():
            lines.append(f"           {key}: {value!r}")
        if live is not None and row.mounted is not None:
            status = "mounted" if row.mounted.active else "parked"
            lines.append(f"           -- {status}")

    if live is None:
        lines.append("")
        lines.append("# static dump — nothing was mounted. Services each row")
        lines.append("# provides are listed under `gangge --show-seams`.")
        return "\n".join(lines)

    described = live.ctx.describe()
    lines.append("")
    lines.append("services:")
    for key in sorted(described["services"]):
        lines.append(f"  {key:<14} {described['services'][key]}")
    if live.missing():
        lines.append(f"  !! missing core services: {', '.join(live.missing())}")
    if live.failed:
        for row_id, error in live.failed:
            lines.append(f"  !! {row_id}: {error}")

    if described["events"]:
        lines.append("")
        lines.append("listeners:")
        for event, count in sorted(described["events"].items()):
            lines.append(f"  {event:<22} {count}")

    return "\n".join(lines)


def list_profiles(workspace: str | Path | None = None) -> list[str]:
    return available_profiles(Path(workspace) if workspace else None)


def describe_events() -> str:
    lines = [f"{item['event']:<24} {item['mode']:<10} {item['domain']:<11} {item['summary']}"
             for item in catalogue()]
    return "\n".join(lines)


def describe_seams() -> str:
    lines = []
    for spec in seams.SEAMS:
        impl = ", ".join(spec.builtin) or "(none)"
        lines.append(f"{spec.key:<14} {spec.interface:<18} {spec.summary}  → {impl}")
    return "\n".join(lines)
