"""Built-in plugins — the implementations behind each capability seam.

Each module is a *function-style plugin*: it exposes ``name``, ``inject`` and
``apply(ctx, config)``. Bundles reference them by dotted path, so adding one is
a one-line change in a YAML file rather than an edit to the loop.
"""
