"""生命周期 Hook 系统（#0013）。

在 Agent 生命周期关键节点挂声明式自动化动作（格式化、安全策略、上下文注入、外部通知）。
对外主入口：``build_hook_manager(base_dir)`` 加载两档 hooks.yaml、构造就绪的 ``HookManager``；
无配置 / 加载失败时返回 None（静默退化为「无 Hook」，所有挂点 no-op）。
"""

import logging
from pathlib import Path

from coreagent.hooks.loader import load_hooks
from coreagent.hooks.manager import HookManager, HookOutcome, HookRuntime
from coreagent.hooks.models import (
    PERMISSION_DENIED,
    POST_COMPACT,
    POST_TOOL_USE,
    POST_TOOL_USE_FAILURE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SESSION_END,
    SESSION_START,
    STOP,
    USER_PROMPT_SUBMIT,
    Action,
    Hook,
    HookConfigError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "build_hook_manager",
    "HookManager",
    "HookOutcome",
    "HookRuntime",
    "Hook",
    "Action",
    "HookConfigError",
    "load_hooks",
    "SESSION_START",
    "SESSION_END",
    "USER_PROMPT_SUBMIT",
    "STOP",
    "PRE_TOOL_USE",
    "POST_TOOL_USE",
    "POST_TOOL_USE_FAILURE",
    "PERMISSION_DENIED",
    "PRE_COMPACT",
    "POST_COMPACT",
]


def build_hook_manager(base_dir: Path | str | None = None) -> HookManager | None:
    """加载两档 hooks.yaml 并装配 HookManager；无规则 / 整体失败 → None（退化为无 Hook）。

    fail-soft：任何加载异常都记中文告警并返回 None，绝不阻断 Agent 启动。
    """
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    try:
        hooks = load_hooks(base)
    except Exception as e:  # noqa: BLE001 —— 加载整体兜底：失败退化为无 Hook
        logger.warning("Hook 系统加载失败，已退化为无 Hook：%r", e)
        return None
    if not hooks:
        return None
    return HookManager(hooks)
