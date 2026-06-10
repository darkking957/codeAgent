"""T1｜团队数据模型：Team / Member / Task / Message。

仿 #0014 `coreagent/agents/types.py`（AgentRole dataclass）与 #0012 技能类型层。四个对象都只是
**纯数据容器**：序列化 / 落盘 / 锁由 store / tasks / mailbox 各层负责，模型本身不做 IO。

字段名 → 落盘键名的映射集中在各 store 层（用 constants 的字段名常量），故这里用 Python 习惯的
下划线属性名，落盘时再转 kebab-case 键（如 ``agent_id`` → ``agent-id``）。
"""

from dataclasses import dataclass, field

from coreagent.teams import constants


@dataclass
class Member:
    """一个长驻队员的身份与运行配置（落 config.json 的 members 数组）。"""

    name: str                                   # 队员名（点对点消息的收件人键、认领标识）
    role: str | None = None                     # 复用的 #0014 子 Agent 角色名（None = Fork 式无角色）
    agent_id: str = ""                          # 后台管理器分配的 agent id（落 agent-id）
    agent_type: str = constants.BACKEND_IN_PROCESS  # 运行后端（落 agent-type；本期恒 in-process）
    cwd: str = ""                               # 工作目录（隔离时为 worktree 路径）
    needs_approval: bool = False                # 是否需 Lead 审批（起手在只读 plan 模式）
    # ── 运行态（系统维护、用户不手改）──────────────────────────────────────────────
    session_id: str = ""                        # 运行会话 id（落 session-id）
    pane_id: str = ""                            # split-pane 窗格 id（落 pane-id；本期占位空）


@dataclass
class Team:
    """一个长期存在的小组（落 ~/.codeagent/teams/{name}/config.json）。"""

    name: str                                   # 团队名（目录名 / 任务命名空间）
    lead: str = ""                              # 负责人标识（建团队的会话身份，终身固定）
    members: list[Member] = field(default_factory=list)   # 成员花名册
    backend: str = constants.BACKEND_AUTO       # 运行后端选择（auto/in-process/split-pane）

    def member(self, name: str) -> Member | None:
        """按名字查队员（名称注册表查询）；无则 None。"""
        for m in self.members:
            if m.name == name:
                return m
        return None

    def member_names(self) -> list[str]:
        return [m.name for m in self.members]


@dataclass
class Task:
    """一条共享任务（落 ~/.codeagent/tasks/{team}/{id}.json）。"""

    id: str
    title: str = ""
    deps: list[str] = field(default_factory=list)   # 依赖的任务 id；全 done → 本任务解锁
    assignee: str | None = None                     # 指派给某队员（None / "" = 未指派，谁都可领）
    files: list[str] = field(default_factory=list)  # 负责的文件集（分文件集避免互踩）
    status: str = constants.TASK_BLOCKED            # blocked/pending/in_progress/done
    claimed_by: str | None = None                   # 认领者队员名（落 claimed-by）
    created: float = 0.0                            # 创建时间戳（写入侧生成）
    updated: float = 0.0                            # 末次更新时间戳


@dataclass
class Message:
    """一条邮箱消息（落收件人邮箱文件的一项）。"""

    sender: str                                  # 发件人（落 from）
    body: str                                    # 正文
    timestamp: float = 0.0                       # 时间戳（落盘自动补，写入侧生成）
    read: bool = False                           # 是否已读（落盘默认 False = 未读）
    summary: str = ""                            # 摘要（可空）
    kind: str | None = None                      # 结构化协议消息类型（普通消息为 None）
