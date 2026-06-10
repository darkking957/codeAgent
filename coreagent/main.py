import argparse
import asyncio
import logging
import os
import sys

from coreagent.agents import build_agent_tool
from coreagent.commands import build_command_registry
from coreagent.config import load_config
from coreagent.context import ContextManager
from coreagent.conversation import Conversation
from coreagent.environment import build_env_block
from coreagent.errors import ConfigError
from coreagent.hooks import build_hook_manager
from coreagent.mcp import build_mcp_manager
from coreagent.memory.instructions import build_instructions_block
from coreagent.memory.notes import NotesStore
from coreagent.memory.session_summary import SessionMemory
from coreagent.permissions import build_pipeline
from coreagent.prompts import build_system_prompt
from coreagent.providers import create_provider
from coreagent.skills.loader_tool import LoadSkillTool
from coreagent.skills.store import SkillStore
from coreagent.teams import build_team_subsystem, teams_enabled
from coreagent.tools import build_registry
from coreagent.tui import TUI

# 日志级别由环境变量控制，默认安静（WARNING），且写 stderr 不污染对话区（stdout）。
_LOG_ENV = "COREAGENT_LOG_LEVEL"


def _configure_logging() -> None:
    level_name = os.environ.get(_LOG_ENV, "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="CoreAgent — Terminal AI Assistant")
    parser.add_argument("--config", default="config.yaml", help="YAML 配置文件路径")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.debug("配置文件缺失", exc_info=True)
        print(str(e))
        sys.exit(1)
    except ConfigError as e:
        logger.debug("配置校验失败", exc_info=True)
        print(f"配置错误：{e}")
        sys.exit(1)

    try:
        provider = create_provider(config)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    # 注入工具注册中心与系统提示词；环境块在启动时快照一次（git 子进程只调一次），整会话复用。
    registry = build_registry()
    # MCP 接入（#0008）：在 build_registry 之后、build_system_prompt 之前装配——把发现到的远端
    # 工具适配为本仓 Tool 注册进 registry，使其工具名进入系统提示词。fail-soft：连不上的 server
    # 记中文告警并跳过，Agent 用已连上的工具照常启动。无任何 server 配置时返回 None（零开销）。
    try:
        mcp_manager = build_mcp_manager(registry)
    except Exception as e:  # noqa: BLE001 —— MCP 装配整体兜底，绝不阻断 Agent 启动
        logger.warning("MCP 装配失败，已跳过：%r", e)
        mcp_manager = None
    # 技能系统（#0012）：在 registry（含 MCP 工具）就绪后构建技能仓——三级发现 + 白名单校验
    # （对照 registry.names() ∪ load_skill ∪ 各技能自带工具名）；再把系统级 loader 工具 load_skill
    # 注册进 registry（恒在、免白名单），使其进入系统提示词与每轮工具列表。fail-soft：整体失败
    # 记中文告警并降级为无技能系统，绝不阻断 Agent 启动。
    skills = None
    try:
        skills = SkillStore(registry, project_dir=os.getcwd())
        registry.register(LoadSkillTool(skills))
    except Exception as e:  # noqa: BLE001 —— 技能装配整体兜底，绝不阻断 Agent 启动
        logger.warning("技能系统装配失败，已跳过：%r", e)
        skills = None
    # 子 Agent 委派系统（#0014）：装配角色仓 + 后台管理器 + 统一委派工具 spawn_agent，在
    # build_system_prompt **之前**注册（名字才进系统提示与每轮工具列表，且对主 Agent schema 稳定）。
    # conversation / system_prompt / hooks 在其后才就绪，经 agent_tool.bind(...) 回填。
    # fail-soft：整体失败记中文告警并降级为无委派系统，绝不阻断 Agent 启动。
    # Git Worktree 隔离（#0015）：构建工作树管理器（声明 isolation=worktree 的角色委派据此建隔离
    # 工作区）。enabled=False 时为 None——声明隔离的角色委派将硬失败（绝不静默回退共享工作区）。
    # 同一实例传给委派工具（建/收尾）与 TUI（周期清理），用绝对路径隔离、不 chdir。
    worktree_manager = None
    if config.worktree.enabled:
        try:
            from coreagent.worktree import WorktreeManager

            worktree_manager = WorktreeManager(os.getcwd(), config.worktree)
        except Exception as e:  # noqa: BLE001 —— 装配兜底，绝不阻断 Agent 启动
            logger.warning("worktree 隔离子系统装配失败，已跳过：%r", e)
            worktree_manager = None
    agent_tool = None
    background_manager = None
    role_store = None
    try:
        agent_tool, role_store, background_manager = build_agent_tool(
            provider=provider,
            config=config,
            registry=registry,
            conversation=None,
            system_prompt=None,
            base_dir=os.getcwd(),
            worktree_manager=worktree_manager,
        )
        registry.register(agent_tool)
    except Exception as e:  # noqa: BLE001 —— 委派系统装配整体兜底，绝不阻断 Agent 启动
        logger.warning("子 Agent 委派系统装配失败，已跳过：%r", e)
        agent_tool = None
        background_manager = None
    # Agent Teams（#0016）：实验开关开启时装配团队子系统并注册团队工具——须在 build_system_prompt
    # **之前**（工具名才进系统提示与每轮工具列表）。关闭时完全不装（主循环 / 工具列表 / 键位与现状
    # 逐字节一致）。team_manager 的迟到依赖（hooks / lead_mode_getter）随后回填（仿 #0014 bind 时序）。
    # fail-soft：整体失败记中文告警并降级为无团队系统，绝不阻断 Agent 启动。
    team_manager = None
    if teams_enabled():
        try:
            if background_manager is None:
                from coreagent.agents import BackgroundAgentManager

                background_manager = BackgroundAgentManager()
            team_manager, team_tools = build_team_subsystem(
                provider=provider,
                config=config,
                registry=registry,
                background_manager=background_manager,
                role_store=role_store,
                hooks=None,
                base_dir=os.getcwd(),
                worktree_manager=worktree_manager,
            )
            for tool in team_tools:
                registry.register(tool)
        except Exception as e:  # noqa: BLE001 —— 团队系统装配整体兜底，绝不阻断 Agent 启动
            logger.warning("团队子系统装配失败，已跳过：%r", e)
            team_manager = None
    system_prompt = build_system_prompt(registry.names())
    env_block = build_env_block()
    # 授权流水线（#0007）：加载四作用域 + 预置白名单，装配 pre-filter / 引擎 / 审计。
    pipeline = build_pipeline()
    # 斜杠命令注册中心（#0011）：登记内置命令；命令名 / 别名冲突属编码错误，fail-closed
    # 中文报错 + 退出（沿用配置非法的退出范式），不拖到运行时。
    try:
        commands = build_command_registry()
    except ValueError as e:
        print(f"命令注册错误：{e}")
        sys.exit(1)

    # 生命周期 Hook 系统（#0013）：加载两档 hooks.yaml、构造 HookManager（单一实例贯穿会话）。
    # 无 hooks.yaml / 加载失败 → None（静默退化为「无 Hook」，所有挂点 no-op），绝不阻断启动。
    try:
        hooks = build_hook_manager(os.getcwd())
    except Exception as e:  # noqa: BLE001 —— Hook 装配整体兜底，绝不阻断 Agent 启动
        logger.warning("Hook 系统装配失败，已跳过：%r", e)
        hooks = None

    conversation = Conversation.load()
    # 回填子 Agent 委派工具的迟到依赖（#0014）：会话历史（Fork 克隆源）、系统提示（Fork 的
    # system / 缓存命中前缀）、Hook 管理器（per-agent 运行时来源）。
    if agent_tool is not None:
        agent_tool.bind(conversation=conversation, system_prompt=system_prompt, hooks=hooks)
    # 回填团队枢纽的迟到依赖（#0016）：Hook 管理器（队员 per-agent Hook 运行时来源）。
    # lead_mode_getter（读 Lead 当前权限模式，队员 spawn 时继承）在 TUI 构造后回填（见下）。
    if team_manager is not None:
        team_manager.hooks = hooks
    # 上下文管理器（#0009）：持 provider/config，每请求前两层压缩 + 实时 token 统计。
    # 接入 Hook（#0013）：PreCompact / PostCompact 挂点。
    context = ContextManager(provider, config, hooks=hooks)

    # 记忆系统（#0010）：项目指令块（system 尾部块）+ 会话摘要存档/恢复 + 自动笔记。
    # 任一环节装配失败软化（记中文告警、降级为无该能力），绝不阻断 Agent 启动。enabled=False 时整体跳过。
    instructions_block: str | None = None
    session_memory = None
    notes_store = None
    if config.memory.enabled:
        project_dir = os.getcwd()
        try:
            instructions_block = (
                build_instructions_block(project_dir, max_depth=config.memory.include_max_depth)
                or None
            )
        except Exception as e:  # noqa: BLE001 —— 指令文件装配兜底
            logger.warning("项目指令文件加载失败，已跳过：%r", e)
        try:
            session_memory = SessionMemory(project_dir, config, provider)
            notes_store = NotesStore(project_dir, config)
        except Exception as e:  # noqa: BLE001 —— 会话记忆 / 笔记装配兜底
            logger.warning("会话记忆 / 笔记装配失败，已跳过：%r", e)
            session_memory = None
            notes_store = None

    tui = TUI(provider, config, conversation, registry, system_prompt, env_block,
              pipeline=pipeline, mcp_manager=mcp_manager, context=context,
              instructions_block=instructions_block, session_memory=session_memory,
              notes_store=notes_store, commands=commands, skills=skills, hooks=hooks,
              background_manager=background_manager, worktree_manager=worktree_manager,
              team_manager=team_manager)
    # 队员 spawn 时继承 Lead 当前权限模式（#0016）：lead_mode_getter 读 TUI 的实时 self.mode。
    if team_manager is not None:
        team_manager._lead_mode_getter = lambda: tui.mode
    asyncio.run(tui.run())
