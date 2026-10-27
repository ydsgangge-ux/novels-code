"""Gangge runtime — a plugin tree instead of a monolithic agent.

The five layers (``layer1_ui`` … ``layer5_llm``) still hold the implementation.
What this package adds is the *assembly*: which of those implementations is
active is decided by a profile, and every registration is a reversible side
effect scoped to a plugin.

Quick start::

    from gangge.runtime import bootstrap

    runtime = bootstrap("standard", workspace=".")
    tools = runtime.tools          # ctx.tools
    llm = runtime.llm              # ctx.llm
    runtime.ctx.on("turn/end", my_listener)   # subscribe to the lifecycle

    print(runtime.dump_config())   # what actually got mounted, and why

Write your own plugin as a module with three attributes::

    # gangge_myplugin.py
    name = "myplugin"
    inject = ("tools",)            # required services; blocks until available

    def apply(ctx, config):
        ctx.on("tools/pre-execute", lambda invoke, next: (
            invoke.veto("blocked") if invoke.tool == "bash" else next(invoke)
        ))
"""

from gangge.runtime.boot import (
    BootError,
    Runtime,
    bootstrap,
    describe_events,
    describe_seams,
    dump_tree,
    list_profiles,
)
from gangge.runtime.context import (
    Context,
    CycleError,
    Inject,
    MissingServiceError,
    Mount,
    Plugin,
    Service,
)
from gangge.runtime.patch import PatchError, Row, Tree, load_document, parse_ops
from gangge.runtime.pipeline import tool_pipeline
from gangge.runtime.profile import (
    Profile,
    ProfileError,
    Resolved,
    available_profiles,
    bundle_path,
    load_profile,
    resolve,
)
from gangge.runtime import events, seams

__all__ = [
    # boot
    "bootstrap",
    "Runtime",
    "BootError",
    "list_profiles",
    "describe_events",
    "describe_seams",
    "dump_tree",
    # container
    "Context",
    "Inject",
    "Service",
    "Plugin",
    "Mount",
    "MissingServiceError",
    "CycleError",
    # composition
    "resolve",
    "load_profile",
    "available_profiles",
    "bundle_path",
    "Profile",
    "Resolved",
    "ProfileError",
    "Tree",
    "Row",
    "parse_ops",
    "load_document",
    "PatchError",
    "tool_pipeline",
    # catalogues
    "events",
    "seams",
]
