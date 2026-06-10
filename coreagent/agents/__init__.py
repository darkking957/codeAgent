"""子 Agent 委派系统（#0014）。

把子任务委派给独立子 Agent：干净上下文、受限工具集、独立权限追踪、可后台真并行，跑完只把
结果送回主对话。对外主入口：``build_agent_tool(...)`` 装配统一委派工具 spawn_agent。

模块：
  constants  固定值（目录 / 字段名 / 工具名 / 标签 / 阈值）
  types      AgentRole + 解析异常
  parser     角色文件解析（frontmatter + 系统提示正文）
  discovery  四级发现 + 同名覆盖（插件级预留不扫描）
  store      AgentRoleStore（发现映射 + 查询 + reload）
  runtime    子 Agent 状态隔离工厂 + 多层工具过滤
  runner     跑到底（复用 #0004 Agent Loop）+ 非交互权限 + 用量回收
  background 后台管理器（真并行 + 状态/用量）+ 系统提示后缀通道
  tool       统一委派工具 spawn_agent（类型分流 + Fork 克隆 + 三种进后台）
"""

from coreagent.agents.background import BackgroundAgentManager
from coreagent.agents.constants import DELEGATION_TOOL_NAME
from coreagent.agents.runner import SubAgentResult, run_subagent
from coreagent.agents.runtime import build_subagent_runtime, compute_tool_filter
from coreagent.agents.store import AgentRoleStore
from coreagent.agents.tool import AgentTool
from coreagent.agents.types import AgentParseError, AgentRole

__all__ = [
    "AgentRole",
    "AgentParseError",
    "AgentRoleStore",
    "BackgroundAgentManager",
    "AgentTool",
    "SubAgentResult",
    "run_subagent",
    "build_subagent_runtime",
    "compute_tool_filter",
    "DELEGATION_TOOL_NAME",
    "build_agent_tool",
]


def build_agent_tool(
    *,
    provider,
    config,
    registry,
    conversation,
    system_prompt,
    role_store=None,
    background_manager=None,
    hooks=None,
    base_dir=None,
    worktree_manager=None,
):
    """装配 spawn_agent 委派工具（连同角色仓 / 后台管理器）。返回 (tool, role_store, bg_manager)。

    role_store / background_manager 缺省自建；调用方（main）随后把 tool 注册进 registry、把
    bg_manager 绑定到事件循环（TUI run() 启动时 bind_loop）。
    ``worktree_manager``（#0015）：声明隔离的角色委派时建工作树；None = 未启用隔离（隔离角色硬失败）。
    """
    if role_store is None:
        role_store = AgentRoleStore(project_dir=base_dir)
    if background_manager is None:
        background_manager = BackgroundAgentManager()
    tool = AgentTool(
        provider=provider,
        config=config,
        registry=registry,
        role_store=role_store,
        background_manager=background_manager,
        conversation=conversation,
        system_prompt=system_prompt,
        hooks=hooks,
        base_dir=base_dir,
        worktree_manager=worktree_manager,
    )
    return tool, role_store, background_manager
