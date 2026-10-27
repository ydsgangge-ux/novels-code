"""Context — the plugin container that the whole runtime is assembled from.

Adapted from DeepSeek Harness / Cordis ("everything is a plugin"). Five rules:

1. A **plugin** is an object with an optional ``inject`` and an ``apply(ctx, config)``.
   A ``Service`` subclass is also a plugin, and mounting it publishes ``ctx.<name>``.
2. The **context** is the service container. A service occupies a stable key
   (``ctx.tools``, ``ctx.llm``, ``ctx.sessions`` ...); consumers look it up by
   key instead of importing a concrete implementation.
3. ``inject`` declares dependencies. A plugin whose dependencies are not
   available yet is *parked* and activated later — load order is expressed by
   service dependencies, never by hand-written start-up sequences.
4. **Typed events** are the communication channel. Five dispatch modes, each
   with exactly one dispatch method (see ``events.py``).
5. **Registrations are reversible side effects.** Disposers installed through
   ``ctx.effect()`` / ``ctx.on()`` / ``ctx.provide()`` run in LIFO order when
   the plugin is unloaded, so there is no privileged kernel to patch.
"""

from __future__ import annotations

import asyncio
import logging
from collections import ChainMap
from contextlib import contextmanager
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Iterable,
    Iterator,
    MutableMapping,
    Protocol,
    runtime_checkable,
)

logger = logging.getLogger(__name__)

# A disposer is any zero-argument callable that undoes a side effect.
Disposer = Callable[[], None]

# Any value a listener may return.
Listener = Callable[..., Any]


class MissingServiceError(RuntimeError):
    """Raised when a required service is not registered."""

    def __init__(self, key: str, requester: str = ""):
        self.key = key
        self.requester = requester
        hint = f" (required by '{requester}')" if requester else ""
        super().__init__(f"Service '{key}' is not available{hint}")


class CycleError(RuntimeError):
    """Raised when a service provider depends on itself."""


# ── Plugin protocol ─────────────────────────────────────────────


@runtime_checkable
class Plugin(Protocol):
    """Structural type for a function-style plugin."""

    def apply(self, ctx: "Context", config: dict[str, Any]) -> None | Awaitable[None]:
        ...


class Inject:
    """Declared service dependencies.

    ``required`` services must exist before ``apply`` runs. ``optional`` ones
    are looked up with ``ctx.get()`` and never block activation::

        class MyPlugin:
            inject = Inject(required=("tools",), optional=("telemetry",))

    A plain tuple is accepted as shorthand for "all required".
    """

    __slots__ = ("required", "optional")

    def __init__(
        self,
        required: Iterable[str] = (),
        optional: Iterable[str] = (),
    ) -> None:
        self.required = tuple(required)
        self.optional = tuple(optional)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Inject(required={self.required!r}, optional={self.optional!r})"


def _normalize_inject(spec: Any) -> Inject:
    if spec is None:
        return Inject()
    if isinstance(spec, Inject):
        return spec
    if isinstance(spec, dict):
        return Inject(required=spec.get("required", ()), optional=spec.get("optional", ()))
    if isinstance(spec, (tuple, list, set)):
        return Inject(required=tuple(spec))
    raise TypeError(f"Unsupported inject declaration: {spec!r}")


class Service:
    """Base class for class-style plugins that publish a service.

    Subclasses set ``name`` (the ``ctx`` key) and usually ``inject``.  Mounting
    the class instantiates it as ``cls(ctx, config)`` and publishes it as
    ``ctx.<name>``.  Resources acquired in ``__init__`` should be released by
    overriding ``stop()`` — it is registered as a disposer automatically.
    """

    name: ClassVar[str] = ""
    inject: ClassVar[Any] = Inject()

    def __init__(self, ctx: "Context", config: dict[str, Any] | None = None) -> None:
        self.ctx = ctx
        self.config = config or {}

    def stop(self) -> None:  # pragma: no cover - default no-op
        """Release resources. Runs on unload, after every later effect."""


# ── Mount bookkeeping ───────────────────────────────────────────


class Mount:
    """A plugin mounted on a context, possibly still parked on dependencies."""

    __slots__ = ("plugin", "config", "ctx", "active", "context", "_disposers")

    def __init__(self, plugin: Any, config: dict[str, Any], ctx: "Context") -> None:
        self.plugin = plugin
        self.config = config
        self.ctx = ctx
        self.active = False
        self.context: Context | None = None
        self._disposers: list[Disposer] = []

    @property
    def name(self) -> str:
        return getattr(self.plugin, "name", None) or getattr(self.plugin, "__name__", repr(self.plugin))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        state = "active" if self.active else "parked"
        return f"<Mount {self.name} {state}>"


# ── Context ─────────────────────────────────────────────────────


class Context:
    """Service container + event bus + reversible effect registry.

    Contexts form a tree: a child sees every service published by its
    ancestors, and services it publishes are invisible to its parent.  Each
    mounted plugin gets its own child context, which is what makes unload
    complete — disposing that child drops the plugin's services, listeners and
    effects without touching anyone else's.
    """

    def __init__(self, name: str = "root", parent: "Context | None" = None) -> None:
        self.name = name
        self.parent = parent
        # Reads fall through to ancestors, writes stay local — boot data such as
        # the workspace is set on the root and visible everywhere below it.
        self.data: MutableMapping[str, Any] = ChainMap() if parent is None else parent.data.new_child()
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Listener]] = {}
        self._mounts: list[Mount] = []
        self._all_mounts: list[Mount] = []   # populated on the root only
        self._disposers: list[Disposer] = []
        self._disposed = False
        # A plugin's own context publishes upwards so its service is reachable
        # from the root, while the disposer stays bound to the plugin's scope.
        self._publishes_up = False

    # ---- service registry -------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Look a service up by key, walking up the context tree."""
        ctx: Context | None = self
        while ctx is not None:
            if key in ctx._services:
                return ctx._services[key]
            ctx = ctx.parent
        return default

    def require(self, key: str) -> Any:
        """Look a service up by key or raise ``MissingServiceError``."""
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise MissingServiceError(key)
        return value

    def has(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def provide(self, key: str, value: Any) -> Disposer:
        """Publish ``value`` as ``ctx.<key>``. Returns a disposer.

        A plugin's ``apply`` runs on its own child context, but the service it
        provides must be reachable from the root — so the value is written to
        the nearest enclosing scope while the disposer stays bound to the
        plugin, which is what makes unload complete.

        Publishing wakes any plugin that was parked waiting for this service,
        in mount order — this is the whole dependency-resolution mechanism.
        """
        target = self._publish_target()
        if target._disposed:
            raise RuntimeError(f"cannot provide '{key}' on a disposed context")
        if key in target._services:
            raise KeyError(f"Service '{key}' is already provided by '{target.name}'")

        target._services[key] = value

        def unpublish() -> None:
            if target._services.get(key) is value:
                target._services.pop(key, None)

        self.effect(unpublish)
        target._resume_parked()
        logger.debug("[ctx] %s provides '%s'", self.name, key)
        return unpublish

    def _publish_target(self) -> "Context":
        """Walk out of plugin scopes to the context services belong to."""
        ctx = self
        while ctx._publishes_up and ctx.parent is not None:
            ctx = ctx.parent
        return ctx

    def root(self) -> "Context":
        ctx = self
        while ctx.parent is not None:
            ctx = ctx.parent
        return ctx

    def consume(self, **services: Any) -> dict[str, Any]:
        """Batch variant of ``provide`` — returns one disposer for all of them."""
        disposers = [self.provide(k, v) for k, v in services.items()]
        return {"dispose": lambda: [d() for d in reversed(disposers)]}

    # ---- reversible effects -----------------------------------

    def effect(self, disposer: Disposer) -> Disposer:
        """Register a cleanup callable; it runs in LIFO order on unload."""
        if not callable(disposer):
            raise TypeError("effect() expects a zero-argument callable")
        self._disposers.append(disposer)
        return disposer

    def on_cleanup(self, disposer: Disposer) -> Disposer:
        """Alias of ``effect`` — reads better for resource acquisition."""
        return self.effect(disposer)

    def dispose(self) -> None:
        """Unload every plugin and run all disposers, newest first.

        Prefer ``await aclose()`` when any disposer is asynchronous; here a
        coroutine returned by a disposer is scheduled on the running loop, or
        closed with a warning when there is none.
        """
        if self._disposed:
            return
        self._disposed = True
        for mount in reversed(self._mounts):
            _unload(mount)
        self._mounts.clear()
        while self._disposers:
            fn = self._disposers.pop()
            try:
                result = fn()
                _settle(result, f"disposer of ctx:{self.name}")
            except Exception:  # a broken disposer must not block the rest
                logger.exception("[ctx:%s] disposer failed", self.name)
        self._services.clear()
        self._listeners.clear()

    async def aclose(self) -> None:
        """Async variant of ``dispose`` — awaits every awaitable disposer."""
        if self._disposed:
            return
        self._disposed = True
        for mount in reversed(self._mounts):
            await _aunload(mount)
        self._mounts.clear()
        while self._disposers:
            fn = self._disposers.pop()
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("[ctx:%s] async disposer failed", self.name)
        self._services.clear()
        self._listeners.clear()

    def sub(self, name: str) -> "Context":
        """Create a child context without mounting a plugin on it."""
        return Context(name=name, parent=self)

    @contextmanager
    def scope(self, name: str = "scope") -> Iterator["Context"]:
        """Child context that is disposed when the ``with`` block exits."""
        child = self.sub(name)
        try:
            yield child
        finally:
            child.dispose()

    # ---- plugin mounting --------------------------------------

    def plugin(self, plugin: Any, config: dict[str, Any] | None = None) -> Mount:
        """Mount a plugin. Returns the mount handle.

        If ``inject.required`` is not satisfied yet the mount stays parked and
        is activated automatically once those services appear.
        """
        if self._disposed:
            raise RuntimeError(f"cannot mount '{_plugin_name(plugin)}' on a disposed context")
        mount = Mount(plugin, config or {}, self)
        self._mounts.append(mount)
        self.root()._all_mounts.append(mount)
        _try_activate(mount)
        return mount

    def use(self, *plugins: Any, configs: dict[str, dict[str, Any]] | None = None) -> list[Mount]:
        """Mount several plugins in order."""
        configs = configs or {}
        return [self.plugin(p, configs.get(_plugin_name(p), {})) for p in plugins]

    def _resume_parked(self) -> None:
        """Retry every parked mount in the tree, not just this context's own."""
        for mount in list(self.root()._all_mounts):
            if not mount.active and mount.ctx is not None and not mount.ctx._disposed:
                _try_activate(mount)

    # ---- events -----------------------------------------------

    def on(self, event: str, listener: Listener, prepend: bool = False) -> Disposer:
        """Register an event listener. Returns a disposer.

        Like ``provide``, the registration lands on the enclosing scope so that
        sibling plugins subscribed to the same event can see each other — a
        guard plugin must be able to intercept the tool service's own
        ``tools/pre-execute``. The disposer stays bound to this context, so the
        listener is still removed when the plugin unloads.

        Use ``ctx.sub().on(...)`` when a genuinely private subscription is
        wanted.
        """
        target = self._publish_target()
        bucket = target._listeners.setdefault(event, [])
        if prepend:
            bucket.insert(0, listener)
        else:
            bucket.append(listener)

        def off() -> None:
            try:
                bucket.remove(listener)
            except ValueError:
                pass

        self.effect(off)
        return off

    def once(self, event: str, listener: Listener) -> Disposer:
        """Register a listener that removes itself after the first call."""
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            off()
            return listener(*args, **kwargs)

        off = self.on(event, wrapper)
        return off

    def off(self, event: str) -> None:
        """Drop every listener on an event."""
        self._publish_target()._listeners.pop(event, None)

    def listeners(self, event: str) -> list[Listener]:
        """Listeners visible from here: own scope first, then ancestors."""
        chain: list[Context] = []
        ctx: Context | None = self
        while ctx is not None:
            chain.append(ctx)
            ctx = ctx.parent
        out: list[Listener] = []
        for c in reversed(chain):          # ancestors first
            out.extend(c._listeners.get(event, ()))
        return out

    # -- dispatch mode: emit (observe, no result, not awaited) --

    def emit(self, event: str, *args: Any) -> None:
        for fn in self.listeners(event):
            try:
                fn(*args)
            except Exception:
                logger.exception("[ctx] emit listener failed: %s", event)

    # -- dispatch mode: waterfall (wrap, must call next()) ------

    def waterfall(self, event: str, *args: Any) -> Any:
        """Around-middleware dispatch.

        Each listener receives ``(*args, next)``.  Calling ``next()`` runs the
        downstream listeners and returns their result; returning without
        calling it short-circuits.  With no listeners the first argument is
        returned unchanged.
        """
        listeners = self.listeners(event)

        def step(index: int, *current: Any) -> Any:
            if index >= len(listeners):
                return current[0] if current else None

            def nxt(*replacement: Any) -> Any:
                return step(index + 1, *(replacement if replacement else current))

            return listeners[index](*current, nxt)

        return step(0, *args)

    # -- dispatch mode: bail (stop at first non-None) -----------

    def bail(self, event: str, *args: Any) -> Any:
        for fn in self.listeners(event):
            result = fn(*args)
            if result is not None:
                return result
        return None

    # -- dispatch mode: serial (awaited, in order, last value) --

    async def serial(self, event: str, *args: Any) -> Any:
        result: Any = None
        for fn in self.listeners(event):
            value = fn(*args)
            if asyncio.iscoroutine(value):
                value = await value
            if value is not None:
                result = value
        return result

    # -- dispatch mode: parallel (awaited, fanned out) ---------

    async def parallel(self, event: str, *args: Any) -> None:
        pending = []
        for fn in self.listeners(event):
            value = fn(*args)
            if asyncio.iscoroutine(value):
                pending.append(value)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---- introspection ---------------------------------------

    def describe(self) -> dict[str, Any]:
        """Snapshot of services, listeners and mounts — for ``--dump-config``."""
        ctx: Context | None = self
        services: dict[str, str] = {}
        while ctx is not None:
            for key, value in ctx._services.items():
                services.setdefault(key, _service_repr(value))
            ctx = ctx.parent
        return {
            "name": self.name,
            "services": services,
            "events": {k: len(v) for k, v in self._listeners.items() if v},
            "mounts": [
                {"name": m.name, "active": m.active, "config": m.config}
                for m in self._mounts
            ],
        }

    def __getattr__(self, item: str) -> Any:
        """``ctx.tools`` / ``ctx.llm`` — service lookup as attribute access.

        Only reached when normal attribute lookup fails, so real attributes
        always win. Dunder and private names raise ``AttributeError`` straight
        away, which keeps ``copy``/``pickle``/``repr`` from looping through the
        service registry.
        """
        if item.startswith("_"):
            raise AttributeError(item)
        value = self.get(item, _MISSING)
        if value is _MISSING:
            raise AttributeError(
                f"{self.name!r} has no service '{item}' "
                f"(mounted here: {sorted(self._services)})"
            )
        return value

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Context {self.name} services={sorted(self._services)}>"


_MISSING = object()


# ── module-level helpers ────────────────────────────────────────


def _plugin_name(plugin: Any) -> str:
    return getattr(plugin, "name", None) or getattr(plugin, "__name__", repr(plugin))


def _service_repr(value: Any) -> str:
    return f"{type(value).__module__}.{type(value).__qualname__}"


def _get_inject(plugin: Any) -> Inject:
    return _normalize_inject(getattr(plugin, "inject", None))


def _self_dependency(plugin: Any, inject: Inject) -> str:
    """The key a plugin both publishes and requires, if any.

    Only detectable for ``Service`` subclasses — a function-style plugin
    publishes through ``ctx.provide`` at runtime, so there is nothing static to
    inspect. Catching the class-style case is worth it because the failure mode
    is a silent permanent park.
    """
    if isinstance(plugin, type) and issubclass(plugin, Service) and plugin.name:
        if plugin.name in inject.required:
            return plugin.name
    return ""


def _try_activate(mount: Mount) -> bool:
    """Activate a mount when its required services all exist."""
    if mount.active or mount.ctx._disposed:
        return False

    inject = _get_inject(mount.plugin)
    cycle = _self_dependency(mount.plugin, inject)
    if cycle:
        raise CycleError(
            f"plugin '{mount.name}' requires the service it publishes "
            f"('{cycle}') — it would never activate"
        )

    missing = [key for key in inject.required if not mount.ctx.has(key)]
    if missing:
        logger.debug(
            "[ctx] parked '%s' — waiting for %s", mount.name, ", ".join(missing)
        )
        return False

    child = Context(name=mount.name, parent=mount.ctx)
    child._publishes_up = True
    mount.context = child
    mount.active = True
    logger.debug("[ctx] activating '%s'", mount.name)

    try:
        plugin = mount.plugin
        if isinstance(plugin, type) and issubclass(plugin, Service):
            instance = plugin(child, mount.config)
            stop = getattr(instance, "stop", None)
            if callable(stop):
                child.effect(stop)
            key = plugin.name or mount.name
            child.effect(child.provide(key, instance))
        elif isinstance(plugin, type):
            instance = plugin()
            result = _apply(instance, child, mount.config)
            _absorb(result, child)
        else:
            result = _apply(plugin, child, mount.config)
            _absorb(result, child)
    except Exception:
        logger.exception("[ctx] failed to activate '%s'", mount.name)
        mount.active = False
        child.dispose()
        mount.context = None
        return False

    return True


def _apply(plugin: Any, ctx: Context, config: dict[str, Any]) -> Any:
    """Call ``apply`` with the arity the plugin declares."""
    apply_fn = getattr(plugin, "apply", None)
    if apply_fn is None:
        raise TypeError(f"plugin '{_plugin_name(plugin)}' has no apply(ctx, config)")
    try:
        return apply_fn(ctx, config)
    except TypeError as exc:
        # Tolerate legacy `apply(ctx)` signatures.
        if "argument" not in str(exc):
            raise
        return apply_fn(ctx)


def _absorb(result: Any, ctx: Context) -> None:
    """Accept an awaitable or a disposer returned from ``apply``."""
    if result is None:
        return
    if callable(result) and not asyncio.iscoroutine(result):
        ctx.effect(result)
        return
    if asyncio.iscoroutine(result):
        logger.warning(
            "[ctx] apply() of '%s' returned a coroutine; scheduling it. "
            "Prefer returning a disposer or registering via ctx.effect().",
            ctx.name,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            result.close()
            logger.warning("[ctx] no running loop — coroutine of '%s' discarded", ctx.name)
            return
        loop.create_task(result)


def _settle(result: Any, what: str) -> None:
    """Deal with a disposer that returned a coroutine in a sync context."""
    if not asyncio.iscoroutine(result):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        result.close()
        # Sync teardown of an async resource: the OS reclaims it at exit, and
        # `await runtime.aclose()` is the correct path when it matters.
        logger.debug("[ctx] %s is async but no loop is running — closed unawaited", what)
        return
    loop.create_task(result)


async def _aunload(mount: Mount) -> None:
    if not mount.active:
        return
    mount.active = False
    if mount.context is not None:
        await mount.context.aclose()
        mount.context = None
    logger.debug("[ctx] unloaded '%s'", mount.name)


def _unload(mount: Mount) -> None:
    if not mount.active:
        return
    mount.active = False
    if mount.context is not None:
        mount.context.dispose()
        mount.context = None
    logger.debug("[ctx] unloaded '%s'", mount.name)
