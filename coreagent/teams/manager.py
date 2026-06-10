"""T9/T13｜团队编排枢纽 TeamManager：把各子系统拧到一起、派生长驻队员、维护单团队约束。

持有（一个 Lead 会话一个 TeamManager）：
- ``team_store``（T3）：团队配置 + 单团队约束。
- 当前团队的 ``task_store``（T4）/ ``mailbox``（T5）/ ``messenger`` + ``coordinator``（T7）。
- ``background_manager``（#0014）：把队员长驻协程经同步桥投递到主循环**真并行**。
- ``role_store``（#0014）：按角色名解析队员角色（复用既有子 Agent 角色定义身份）。
- ``lead_mode_getter``：读 Lead 当前权限模式（队员 spawn 时继承）。

派生队员（spawn）：建 Member → 写团队花名册 → 经 background_manager 调度 ``run_member`` 协程。
disband：清当前团队配置 + 取消在跑的队员协程。
"""

import logging

from coreagent.teams import constants
from coreagent.teams.mailbox import Mailbox
from coreagent.teams.messaging import Coordinator, Messenger
from coreagent.teams.store import TeamStore
from coreagent.teams.tasks import TaskStore
from coreagent.teams.types import Member, Team

logger = logging.getLogger(__name__)


class TeamManager:
    """一个 Lead 会话的团队编排枢纽。"""

    def __init__(
        self,
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
    ) -> None:
        self.provider = provider
        self.config = config
        self.registry = registry
        self.background_manager = background_manager
        self.role_store = role_store
        self.hooks = hooks
        self.base_dir = base_dir
        self.worktree_manager = worktree_manager
        self.lead_name = lead_name
        self._lead_mode_getter = lead_mode_getter
        self._teams_root = teams_root
        self._tasks_root = tasks_root

        self.team_store = TeamStore(root=teams_root)
        # 当前团队子系统（create_team 后填充）。
        self.task_store: TaskStore | None = None
        self.mailbox: Mailbox | None = None
        self.messenger: Messenger | None = None
        self.coordinator = Coordinator()
        # 在跑的队员 future（disband / 退出时取消）。
        self._member_futures: dict[str, object] = {}

    # ── Lead 权限模式（队员 spawn 时继承）─────────────────────────────────────────
    def lead_mode(self) -> str:
        from coreagent.permissions import modes
        if self._lead_mode_getter is not None:
            try:
                return self._lead_mode_getter()
            except Exception:  # noqa: BLE001
                pass
        return modes.DEFAULT_MODE

    @property
    def team(self) -> Team | None:
        return self.team_store.current

    # ── 团队生命周期 ────────────────────────────────────────────────────────────
    def create_team(self, name: str, *, backend: str = constants.BACKEND_AUTO) -> Team:
        """建团队（单团队约束在 team_store 内执行）并装配其任务 / 邮箱 / 消息子系统。"""
        team = self.team_store.create_team(name, lead=self.lead_name, backend=backend)
        self.task_store = TaskStore(name, root=self._tasks_root)
        self.mailbox = Mailbox(name, root=self._teams_root)
        self.messenger = Messenger(team, self.mailbox, self.coordinator)
        return team

    def disband(self) -> None:
        """解散当前团队：取消在跑的队员协程 + 清团队配置。"""
        for fut in list(self._member_futures.values()):
            try:
                fut.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._member_futures.clear()
        self.team_store.disband()
        self.task_store = None
        self.mailbox = None
        self.messenger = None

    # ── 派生队员 ────────────────────────────────────────────────────────────────
    def make_member(
        self,
        name: str,
        *,
        role: str | None = None,
        needs_approval: bool = False,
    ) -> Member:
        """构造一个 Member（分配 agent id、解析后端为 in-process）并登记进花名册。"""
        if self.team is None:
            raise ValueError("无当前团队，无法派生队员（请先 create_team）")
        agent_id = self.background_manager.next_id()
        member = Member(
            name=name,
            role=role,
            agent_id=agent_id,
            agent_type=constants.BACKEND_IN_PROCESS,
            needs_approval=needs_approval,
            session_id=agent_id,
        )
        self.team_store.add_member(member)
        # 刷新 messenger 持有的 team 引用（花名册已更新，名称注册表需含新成员）。
        if self.messenger is not None and self.team is not None:
            self.messenger.team = self.team
        return member

    def spawn_member(self, name: str, *, role: str | None = None, needs_approval: bool = False):
        """派生并调度一个 in-process 长驻队员（经同步桥真并行）；返回 (member, future)。"""
        member = self.make_member(name, role=role, needs_approval=needs_approval)
        from coreagent.teams.member import run_member

        coro = run_member(self, member)
        future = self.background_manager.schedule(coro)
        self._member_futures[name] = future
        return member, future

    def resolve_role(self, role_name: str | None):
        """按名解析 #0014 角色（队员复用既有角色身份）；无角色 / 查无 → None。"""
        if not role_name or self.role_store is None:
            return None
        return self.role_store.get(role_name)

    # ── 恢复语义（T13）：in-process 队员不随 /resume 复活 ──────────────────────────
    def is_member_live(self, name: str) -> bool:
        """该队员是否有在跑的 in-process 长驻协程（会话恢复后花名册仍在盘上，但队员不复活）。"""
        fut = self._member_futures.get(name)
        if fut is None:
            return False
        done = getattr(fut, "done", None)
        return not (done() if callable(done) else False)

    def reattach(self, name: str) -> Team | None:
        """会话恢复后重挂到盘上已存在的团队配置（载入花名册，但**不**复活队员）。无则 None。"""
        team = self.team_store.load(name)
        if team is None:
            return None
        self.team_store._current = team   # 直接挂载（绕过单团队约束的「新建」校验）
        from coreagent.teams.mailbox import Mailbox
        from coreagent.teams.messaging import Messenger
        from coreagent.teams.tasks import TaskStore

        self.task_store = TaskStore(name, root=self._tasks_root)
        self.mailbox = Mailbox(name, root=self._teams_root)
        self.messenger = Messenger(team, self.mailbox, self.coordinator)
        return team
