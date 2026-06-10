"""T4/T11｜子 Agent 运行时状态隔离工厂 + 多层工具过滤。

每个子 Agent 拥有**私有**可变状态，互不污染（spec 能力 6 / 非功能「无竞态」）：
- 独立消息历史   ：新建 Conversation（仿 #0012 独立模式 `sub = Conversation()`）。
- 独立权限审计   ：build_pipeline 每次新建实例、独立 AuditLog（#0007）。
- 独立 token 计数：ContextManager 另起实例，锚点不与父共享；且 offload/history 路径隔离到临时目录，
                  绝不写主历史（compaction 即使触发也不破坏父对话）。
- 独立 Hook 运行时：hooks.new_runtime()（#0014 T5），_pending / _once_fired 与主会话隔离。

共享（只读或经命名空间访问，不复制）：LLM 客户端（provider）、Hook 已编译规则集（HookManager）、
工具注册中心（registry，子 Agent 只读不增删工具，故无竞态）、文件系统。

多层工具过滤（T11，防无限嵌套）——基集 = registry 全量工具名，依次：
  ① 全局禁止：剔除委派工具（硬性 depth=1，子 Agent 不能再委派）+ 技能 loader（避免子 Agent
     改写共享 SkillStore 引入竞态）。
  ② 角色白/黑名单：白名单非空 → 取交；黑名单 → 剔除。
  ③ 后台白名单：转后台后 → 再与后台工具白名单取交。
"""

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from coreagent.agents import constants
from coreagent.agents.types import AgentRole
from coreagent.context.manager import ContextManager
from coreagent.conversation import Conversation
from coreagent.permissions import build_pipeline
from coreagent.permissions.pipeline import PermissionPipeline
from coreagent.skills.constants import LOADER_TOOL_NAME

logger = logging.getLogger(__name__)

# 任何子 Agent 都不可见的工具（全局禁止层）：委派工具（depth=1）+ 技能 loader（防改写共享技能仓）。
HIDDEN_FROM_SUBAGENTS = frozenset({constants.DELEGATION_TOOL_NAME, LOADER_TOOL_NAME})


def _team_tool_names() -> set[str]:
    """团队工具名集合（#0016；惰性导入避免与 teams.tools 循环依赖）。

    团队工具仅在团队功能开启时才注册进 registry；普通 #0014 子 Agent **不应**获得它们（保持只向父
    汇报、不能点对点发消息）。队员的工具过滤（teams.tools.compute_member_tool_filter）会显式把协作
    工具加回。功能关闭时这些名字本就不在 registry，故减集为 no-op、对既有行为零影响。
    """
    try:
        from coreagent.teams.constants import ALL_TEAM_TOOLS

        return set(ALL_TEAM_TOOLS)
    except Exception:  # noqa: BLE001 —— teams 不可用则视作无团队工具
        return set()

# 子 Agent 隔离的 offload / history 根目录（临时区；进程退出即丢，绝不混入主历史）。
SUBAGENT_TMP_DIR = Path(tempfile.gettempdir()) / "coreagent-subagents"


def compute_tool_filter(
    registry,
    role: AgentRole | None,
    *,
    background: bool,
) -> set[str]:
    """计算子 Agent 可见工具名集合（三层过滤）；结果交给 run_agent_turn 的 allow_tools。"""
    # 基集 = registry 全量工具名，先做全局禁止（depth=1 + loader + 团队工具）。
    allow = set(registry.names()) - HIDDEN_FROM_SUBAGENTS - _team_tool_names()
    # 角色白/黑名单（Fork 式无角色 → 跳过）。
    if role is not None:
        if role.allowed_tools:
            allow &= set(role.allowed_tools)
        if role.denied_tools:
            allow -= set(role.denied_tools)
    # 后台白名单（转后台后再收窄）。
    if background:
        allow &= set(constants.BACKGROUND_TOOL_ALLOWLIST)
    return allow


@dataclass
class SubAgentRuntime:
    """一个子 Agent 的隔离运行态集合（工厂产出，runner 消费）。"""

    agent_id: str
    conversation: Conversation
    pipeline: PermissionPipeline
    hook_runtime: object | None          # hooks.HookRuntime | None
    context: ContextManager | None
    allow_tools: set[str]
    permission_mode: str
    model_override: str | None
    max_rounds: int | None
    # ── 工作目录隔离（#0015）─────────────────────────────────────────────────────
    cwd: str = ""                        # 本子 Agent 的工作目录绝对路径（透传 run_agent_turn）
    worktree: object | None = None       # worktree.Worktree 句柄（隔离生效时非空；runner 收尾消费）
    worktree_manager: object | None = None  # 收尾 finalize 用（隔离生效时非空）


def build_subagent_runtime(
    agent_id: str,
    *,
    provider,
    config,
    registry,
    role: AgentRole | None,
    permission_mode: str,
    background: bool,
    hooks=None,
    seed_messages: list[dict] | None = None,
    base_dir: Path | str | None = None,
    worktree_manager=None,
) -> SubAgentRuntime:
    """装配一个子 Agent 的全部隔离状态（独立对话 / 权限审计 / token 计数 / Hook 运行时 / 工作目录）。

    ``seed_messages``：Fork 式克隆父历史时传入（置于新对话开头）；定义式为空白对话。
    ``role``：定义式角色（含模型 / 最大轮次 / 白黑名单 / 隔离声明）；Fork 式为 None。
    ``worktree_manager``：worktree 子系统句柄（#0015）。角色声明 ``isolation=worktree`` 时据此建工作树、
    把工作树绝对路径作为 cwd；创建失败 / 未启用 → **抛 WorktreeError 硬失败**（绝不静默回退共享工作区）。
    未声明隔离时 cwd = base_dir（项目根），worktree 句柄为 None。
    """
    # ── 工作目录隔离（#0015）：先定 cwd / worktree 句柄（隔离创建失败在此硬失败、早于建状态）──
    project_root = str(Path(base_dir).resolve()) if base_dir else os.getcwd()
    cwd = project_root
    worktree = None
    if role is not None and role.isolation == constants.ISOLATION_WORKTREE:
        if worktree_manager is None:
            from coreagent.worktree.constants import WorktreeError

            raise WorktreeError(
                f"角色「{role.name}」声明 isolation=worktree，但 worktree 隔离未启用"
                f"（config.worktree.enabled=false 或不在 git 仓），委派硬失败"
            )
        worktree = worktree_manager.create(agent_id)   # 失败抛 WorktreeError，向上硬失败
        cwd = str(worktree.path)

    conversation = Conversation()
    if seed_messages:
        # 纯克隆父历史（含 system 之外的 user/assistant/tool 消息）；逐条浅拷贝避免与父共享引用。
        conversation.messages = [dict(m) for m in seed_messages]

    # 独立权限管线（每次 build_pipeline 新建引擎 + 独立 AuditLog；#0007）。
    pipeline = build_pipeline(base_dir)

    # 独立 Hook 运行时（共享规则、隔离可变态）；无 hooks 时 None（全程 no-op）。
    hook_runtime = hooks.new_runtime() if hooks is not None else None

    # 独立 token 计数：ContextManager 另起实例，offload/history 隔离到临时目录（不碰主历史）。
    context: ContextManager | None = None
    try:
        context = ContextManager(
            provider,
            config,
            offload_dir=SUBAGENT_TMP_DIR / "offload",
            history_path=SUBAGENT_TMP_DIR / f"{agent_id}.json",
            hooks=None,
        )
    except Exception as e:  # noqa: BLE001 —— 上下文管理装配失败软化：降级为无独立计数，不阻断子 Agent
        logger.warning("子 Agent 上下文管理装配失败，已降级：%r", e)
        context = None

    allow_tools = compute_tool_filter(registry, role, background=background)

    return SubAgentRuntime(
        agent_id=agent_id,
        conversation=conversation,
        pipeline=pipeline,
        hook_runtime=hook_runtime,
        context=context,
        allow_tools=allow_tools,
        permission_mode=permission_mode,
        model_override=(role.model if role is not None else None),
        max_rounds=(role.max_turns if role is not None else None),
        cwd=cwd,
        worktree=worktree,
        worktree_manager=(worktree_manager if worktree is not None else None),
    )
