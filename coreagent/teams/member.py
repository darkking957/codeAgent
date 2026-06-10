"""T9/T10｜in-process 队员长驻循环 + 真并行 + 审批流。

复用 #0014 隔离工厂（``build_subagent_runtime``：独立对话 / 权限管线 / token 计数 / Hook 命名空间），
经 #0014 同步桥（``BackgroundAgentManager.schedule``）投递到主循环**真并行**推进。**外层换长驻循环**
（区别于 #0014 一次性 runner）：

  drain 邮箱 → 领 T4 任务 → 跑若干轮 → 标记 done → 继续；无可领任务且无消息 → 标记空闲 → 通知 Lead
  （idle 协议消息）→ await 排队信号唤醒 → 继续。

队员权限模式 spawn 时**继承 Lead 当前模式**（spawn 后可单独改）。**需审批**队员起手在只读 plan 模式：
出计划 → 发 approval-request → park；Lead 驳回（带反馈）→ 留 plan、按反馈改后重交；批准 → 退出 plan
开干（审批状态机见 ``on_approval_reply``，可独立单测）。
"""

import logging
from dataclasses import dataclass

from coreagent.agent import MAX_ROUNDS, run_agent_turn
from coreagent.agents.runner import _make_noninteractive_gate, _reject_confirm
from coreagent.agents.runtime import build_subagent_runtime
from coreagent.events import RunError, TextDelta, TextKind, TurnStart
from coreagent.permissions import modes
from coreagent.permissions.rules import ALLOW, Decision
from coreagent.teams import constants
from coreagent.teams.mailbox import build_inbox_block

logger = logging.getLogger(__name__)

# 队员状态。
RUNNING = "running"
IDLE = "idle"
DONE = "done"
FAILED = "failed"


@dataclass
class MemberResult:
    """一个队员长驻循环结束时的结果（status ∈ running/idle/done/failed）。"""

    name: str
    status: str
    summary: str = ""


def is_approval_body(body: str) -> bool:
    """审批回复正文是否表示「批准」（令牌大小写不敏感）；否则视为驳回、正文即反馈。"""
    return body.strip().lower() == constants.APPROVAL_APPROVE_TOKEN


def _member_gate(pipeline, mode: str):
    """队员门禁：团队协作工具**始终放行**（即使非交互 / plan）；其余走 #0014 非交互门禁。

    协作工具是安全的团队协调动作（任务 CRUD / 发消息），不应被「未命中规则默认 ASK → 非交互
    fail-closed DENY」挡掉——否则队员根本无法领活 / 协作（spec 能力 7「协作工具始终可用」）。
    """
    base = _make_noninteractive_gate(pipeline, mode)

    def gate(tool_name: str, tool_input: dict) -> Decision:
        if tool_name in constants.COLLAB_TOOLS:
            return Decision(ALLOW, stage="team-collab", reason="团队协作工具对队员始终放行")
        return base(tool_name, tool_input)

    return gate


def _member_identity_note(name: str, team_name: str, lead: str, mates: list[str]) -> str:
    """队员身份说明：注入系统提示尾部，告知自己的名字 / 团队 / Lead / 同伴与协作工具用法。"""
    others = "、".join(m for m in mates if m != name) or "（暂无）"
    return (
        f"\n\n【团队身份】你是团队「{team_name}」的队员，你的名字是「{name}」，负责人（Lead）是"
        f"「{lead}」，同伴：{others}。\n"
        f"- 用 team_task_claim(member=\"{name}\") 从共享清单领取任务；做完用 team_task_update 标记 done。\n"
        f"- 用 send_message(**{{\"from\": \"{name}\", \"to\": \"<名字>\"}}, body=...) 给同伴或 Lead 发消息。\n"
        f"- 只动你负责的文件集，避免与同伴互踩。"
    )


def _default_member_system(name: str) -> str:
    return f"你是一个团队队员（名字「{name}」），从共享任务清单领活、与同伴协作、把结果汇报给 Lead。"


def _task_instruction(task) -> str:
    files = "、".join(task.files) if task.files else "（未指定）"
    return (
        f"你领到了任务「{task.id}」：{task.title or '(无标题)'}\n"
        f"负责文件集：{files}\n"
        f"请完成它。完成后用 team_task_update 把该任务状态置为 done，并简述结果。"
    )


class MemberRunner:
    """一个 in-process 队员的长驻执行体（持隔离运行时 + 审批状态机）。"""

    def __init__(self, manager, member, *, max_rounds: int = MAX_ROUNDS, stop_when_idle: bool = False) -> None:
        self.manager = manager
        self.member = member
        self.name = member.name
        self._max_rounds = max_rounds
        # 测试接缝：True 时队员转空闲即返回（不 park），便于单测确定性收敛；默认 False（真长驻）。
        self.stop_when_idle = stop_when_idle

        self.role = manager.resolve_role(member.role)
        # 继承 Lead 当前权限模式（spawn 时）；需审批 → 起手只读 plan。
        inherited = modes.normalize_mode(manager.lead_mode())
        self.mode = modes.PLAN if member.needs_approval else inherited
        self._approved = not member.needs_approval
        self._feedback = ""
        self._shutdown = False
        self.status = RUNNING
        self.summary = ""

        # 隔离运行时（复用 #0014 工厂）：独立对话 / 权限管线 / token 计数 / Hook 命名空间。
        self.runtime = build_subagent_runtime(
            member.agent_id,
            provider=manager.provider,
            config=manager.config,
            registry=manager.registry,
            role=self.role,
            permission_mode=self.mode,
            background=False,
            hooks=manager.hooks,
            seed_messages=None,
            base_dir=manager.base_dir,
            worktree_manager=manager.worktree_manager,
        )
        # 工具集：#0014 隔离过滤 + 团队协作工具（始终并入）− 团队管理工具 − spawn_agent（防嵌套）。
        from coreagent.teams.tools import compute_member_tool_filter

        self.allow_tools = compute_member_tool_filter(manager.registry, self.role)
        # 系统提示：角色正文（或缺省）+ 团队身份说明。
        base = self.role.body if self.role is not None else _default_member_system(self.name)
        mates = manager.team.member_names() if manager.team is not None else []
        self.system = base + _member_identity_note(
            self.name, manager.team.name if manager.team else "", manager.lead_name, mates
        )

    # ── 审批状态机（T10，可独立单测）─────────────────────────────────────────────
    def on_approval_reply(self, approved: bool, feedback: str = "") -> None:
        """处理 Lead 审批回复：批准 → 退出 plan 开干；驳回 → 留 plan、记反馈待重交。"""
        if approved:
            self._approved = True
            self._feedback = ""
            self.mode = modes.DEFAULT      # 退出 plan（可写）
        else:
            self._approved = False
            self._feedback = feedback
            self.mode = modes.PLAN         # 留在 plan
        self.runtime.permission_mode = self.mode

    # ── 长驻循环 ────────────────────────────────────────────────────────────────
    async def run(self) -> MemberResult:
        """跑长驻循环；任何异常都软化为 status=failed（真并行无崩溃，不冒泡崩主循环）。"""
        try:
            await self._loop()
        except Exception as e:  # noqa: BLE001 —— 队员异常不许崩主循环（CancelledError 是 BaseException，照常上抛）
            logger.warning("队员 %s 长驻循环异常：%r", self.name, e)
            self.status = FAILED
            self.summary = f"（队员执行异常：{type(e).__name__}）"
        return MemberResult(self.name, self.status, self.summary)

    async def _loop(self) -> None:
        coord = self.manager.coordinator
        coord.mark_busy(self.name)

        # 需审批 → 起手 plan、走审批流（批准后才开干）。
        if self.member.needs_approval and not self._approved:
            await self._approval_flow()
            if self._shutdown:
                self.status = DONE
                return

        while not self._shutdown:
            normal = self._drain_protocol()
            if self._shutdown:
                break
            task = self.manager.task_store.claim(self.name)
            inbox_block = build_inbox_block(normal)
            if task is None:
                if normal:
                    # 有消息但无任务：跑一轮处理消息（消息已成注入块）。
                    await self._run_turn("（处理收到的团队消息）", inbox_block)
                    continue
                # 无任务无消息 → 空闲 → 通知 Lead → park 等唤醒（或测试接缝直接返回）。
                coord.mark_idle(self.name)
                self.manager.messenger.send_idle(self.name, self.manager.lead_name)
                if self.stop_when_idle:
                    self.status = IDLE
                    return
                await coord.wait_wake(self.name)
                coord.mark_busy(self.name)
                continue
            await self._work_task(task, inbox_block)
        self.status = DONE

    def _drain_protocol(self) -> list:
        """drain 邮箱：分出协议消息（shutdown 置位）与普通消息（返回，供注入块）。"""
        normal = []
        for m in self.manager.mailbox.drain(self.name):
            if m.kind == constants.KIND_SHUTDOWN:
                self._shutdown = True
            elif m.kind == constants.KIND_APPROVAL_REPLY:
                continue   # 审批后阶段收到的审批回复无意义，忽略
            else:
                normal.append(m)
        return normal

    async def _work_task(self, task, inbox_block=None) -> None:
        text = await self._run_turn(_task_instruction(task), inbox_block)
        if text:
            self.summary = text
        # 标记完成（队员也可在 turn 内自行调 team_task_update；此处兜底确保推进、解锁依赖）。
        self.manager.task_store.update(task.id, status=constants.TASK_DONE)

    async def _run_turn(self, instruction: str, inbox_block=None) -> str:
        """跑一轮 Agent Loop（非交互门禁），返回末轮文字。team-inbox 块经 #0016 T8 通道注入。

        若本轮以 RunError 收尾（模型调用退避耗尽等不可恢复错误，run_agent_turn 不上抛而 emit 事件）
        → 抛 RuntimeError，由 ``run`` 兜底为 status=failed（队员死、但不崩主循环）。
        """
        self.runtime.conversation.add_user(instruction)
        gate = _member_gate(self.runtime.pipeline, self.mode)
        parts: list[str] = []
        error: str | None = None
        async for ev in run_agent_turn(
            self.manager.provider,
            self.runtime.conversation,
            self.manager.registry,
            self.system,
            gate=gate,
            confirm=_reject_confirm,
            allow_tools=self.allow_tools,
            model_override=self.runtime.model_override,
            cwd=self.runtime.cwd or None,
            hooks=self.runtime.hook_runtime,
            context=self.runtime.context,
            max_rounds=self._max_rounds,
            plan_only=modes.is_plan(self.mode),
            team_inbox_block=inbox_block,
        ):
            if isinstance(ev, TurnStart):
                parts = []
            elif isinstance(ev, TextDelta) and ev.kind is TextKind.ANSWER:
                parts.append(ev.text)
            elif isinstance(ev, RunError):
                error = ev.error_type
        if error is not None:
            raise RuntimeError(f"队员模型调用失败：{error}")
        return "".join(parts).strip()

    # ── 审批流（T10）────────────────────────────────────────────────────────────
    async def _approval_flow(self) -> None:
        """出计划 → 发 approval-request → park 等审批回复；驳回则改后重交，批准则退出 plan。"""
        while not self._approved and not self._shutdown:
            plan = await self._produce_plan()
            self.manager.messenger.send_approval_request(self.name, self.manager.lead_name, plan)
            reply = await self._park_for_reply()
            if reply is None:        # shutdown 期间被打断
                return
            self.on_approval_reply(reply[0], reply[1])

    async def _produce_plan(self) -> str:
        """在 plan 模式跑一轮产出计划文字（带上次驳回反馈，若有）。"""
        hint = "请先给出实现计划（只规划、不动代码）。"
        if self._feedback:
            hint += f"\n上次计划被驳回，反馈如下，请据此修改后重新给出计划：\n{self._feedback}"
        return await self._run_turn(hint) or "（未产出计划）"

    async def _park_for_reply(self):
        """park 等 Lead 的 approval-reply；返回 (approved, feedback) 或 None（shutdown）。"""
        coord = self.manager.coordinator
        while not self._shutdown:
            for m in self.manager.mailbox.drain(self.name):
                if m.kind == constants.KIND_SHUTDOWN:
                    self._shutdown = True
                    return None
                if m.kind == constants.KIND_APPROVAL_REPLY:
                    return (is_approval_body(m.body), m.body)
            coord.mark_idle(self.name)
            if self.stop_when_idle:
                return None
            await coord.wait_wake(self.name)
            coord.mark_busy(self.name)
        return None


async def run_member(manager, member, **kwargs) -> MemberResult:
    """派生入口：建 MemberRunner 跑长驻循环（manager.spawn_member 经同步桥 schedule 调度本协程）。"""
    runner = MemberRunner(manager, member, **kwargs)
    return await runner.run()
