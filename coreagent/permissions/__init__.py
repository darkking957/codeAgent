"""纵深防御权限系统（#0007）。

授权流水线（pre-filter → 规则引擎 → 分类器插槽）+ 四级作用域 + 五级模式 + HITL + 审计。
对外主入口：``build_pipeline(base_dir)`` 装配就绪的 ``PermissionPipeline``；
``pipeline.decide(tool, input, mode) -> Decision`` 为工具执行前的统一前置门禁。
"""

from pathlib import Path

from coreagent.permissions.audit import AuditLog
from coreagent.permissions.defaults import builtin_allow_rules
from coreagent.permissions.engine import PermissionEngine
from coreagent.permissions.modes import (
    ACCEPT_EDITS,
    AUTO,
    BYPASS,
    DEFAULT,
    DEFAULT_MODE,
    DONT_ASK,
    MODES,
    PLAN,
    available_modes,
    cycle_modes,
    next_mode,
    normalize_mode,
)
from coreagent.permissions.pipeline import PermissionPipeline, prefilter
from coreagent.permissions.rules import (
    ALIAS_TO_TOOL,
    ALLOW,
    ASK,
    DENY,
    TOOL_TO_ALIAS,
    Decision,
    PermissionConfigError,
    Rule,
    parse_rule,
    rule_for_tool_call,
)
from coreagent.permissions.scopes import load_merged, scope_paths

__all__ = [
    "build_pipeline",
    "PermissionPipeline",
    "PermissionEngine",
    "AuditLog",
    "Decision",
    "Rule",
    "PermissionConfigError",
    "parse_rule",
    "rule_for_tool_call",
    "prefilter",
    "load_merged",
    "scope_paths",
    "builtin_allow_rules",
    "ALLOW",
    "ASK",
    "DENY",
    "ALIAS_TO_TOOL",
    "TOOL_TO_ALIAS",
    "MODES",
    "DEFAULT_MODE",
    "PLAN",
    "DEFAULT",
    "ACCEPT_EDITS",
    "AUTO",
    "BYPASS",
    "DONT_ASK",
    "normalize_mode",
    "next_mode",
    "available_modes",
    "cycle_modes",
]


def build_pipeline(base_dir: Path | str | None = None, *,
                   logger=None) -> PermissionPipeline:
    """装配就绪的授权流水线：加载四作用域 + builtin 预置 → 引擎 → 审计 → 流水线。"""
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    merged = load_merged(base, defaults=builtin_allow_rules())
    engine = PermissionEngine(merged)
    audit = AuditLog(logger=logger)
    return PermissionPipeline(engine, audit, base_dir=base)
