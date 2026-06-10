"""Agent Teams（#0016，实验性、默认关闭）。

把主 Agent 升级为 Team Lead：建长期小组、派生长驻队员并行干活，队员经共享任务清单 + 邮箱直接协作
（而非全经 Lead 中转）。建在 #0014（同步桥真并行 / 运行时隔离 / 工具过滤）/ #0007（plan 模式）/
#0006（缓存通道）/ #0015（worktree）/ #0002（原子写）之上。整套由单一环境变量门控、默认关闭，关闭
时行为与现状逐字节一致。

模块：
  constants  固定值（env / 目录 / 状态 / 协议 / 标签 / 锁阈值 / 工具名 / delegate 保留集 / 文案）
  gating     实验门控（读 CODEAGENT_EXPERIMENTAL_AGENT_TEAMS）
  types      Team / Member / Task / Message 数据模型
  locking    文件锁原语 + 原子 JSON 读写
  store      团队配置持久化 + 单团队约束
  tasks      共享任务存储（CRUD + 依赖解锁 + Hybrid 认领）
  mailbox    邮箱 + 消息格式 + 注入块构造
  backend    后端检测 + 能力表（本期仅检测 + 失败语义）
  messaging  点对点投递 + 广播 + 协议消息 + 唤醒协调器
  member     in-process 队员长驻循环 + 审批流
  tools      团队工具集（协作 + 管理）+ 队员工具过滤
  manager    编排枢纽 TeamManager（拧到一起 + 派生队员 + 单团队约束）
"""

from coreagent.teams.gating import teams_enabled

__all__ = ["teams_enabled", "build_team_subsystem"]


def build_team_subsystem(
    *,
    provider,
    config,
    registry,
    background_manager,
    role_store=None,
    hooks=None,
    base_dir=None,
    worktree_manager=None,
    lead_name: str = "lead",
    lead_mode_getter=None,
    teams_root=None,
    tasks_root=None,
):
    """装配团队子系统（仅在实验开关开启时由 main 调用）。返回 (manager, team_tools)。

    把团队工具注册进 registry 由调用方（main）负责（注册前后绑定后台管理器、可见性才正确）。
    ``teams_root`` / ``tasks_root`` 缺省走 ~/.codeagent（见 constants）；传入可把团队 / 任务落盘
    重定向到隔离目录（测试 / e2e 用，避免碰用户家目录）。
    """
    from coreagent.teams.manager import TeamManager
    from coreagent.teams.tools import build_team_tools

    manager = TeamManager(
        provider=provider,
        config=config,
        registry=registry,
        background_manager=background_manager,
        role_store=role_store,
        hooks=hooks,
        base_dir=base_dir,
        worktree_manager=worktree_manager,
        lead_name=lead_name,
        lead_mode_getter=lead_mode_getter,
        teams_root=teams_root,
        tasks_root=tasks_root,
    )
    tools = build_team_tools(manager)
    return manager, tools
