"""把 Qoder CLI 当成一个综合型委派目标。

对外只有四样东西：配置档位、委派契约、执行结果、异步入口。

    from gangge.layer4_tools.qoder_cli import DelegateSpec, delegate

    spec = DelegateSpec(goal="...", scope=("src/a.py",),
                        dod=(CheckSpec("pytest -q"),))
    result = await delegate(spec)          # status: done / partial / violated / failed

独立使用：``python -m gangge.layer4_tools.qoder_cli --help``
"""

from gangge.layer4_tools.qoder_cli.config import (
    EFFORTS,
    MODELS,
    PERMISSIONS,
    STRENGTHS,
    QoderConfig,
    QoderNotAvailable,
    help_flags,
    list_models,
    probe_version,
    resolve_executable,
)
from gangge.layer4_tools.qoder_cli.runner import (
    DelegationResult,
    build_argv,
    delegate,
    delegate_sync,
    parse_output,
)
from gangge.layer4_tools.qoder_cli.spec import CheckSpec, DelegateSpec

__all__ = [
    "EFFORTS",
    "MODELS",
    "PERMISSIONS",
    "STRENGTHS",
    "CheckSpec",
    "DelegateSpec",
    "DelegationResult",
    "QoderConfig",
    "QoderNotAvailable",
    "build_argv",
    "delegate",
    "delegate_sync",
    "help_flags",
    "list_models",
    "parse_output",
    "probe_version",
    "resolve_executable",
]
