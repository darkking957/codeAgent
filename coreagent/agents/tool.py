"""T10/T11｜统一 Agent 委派工具 spawn_agent：类型分流 + Fork 纯克隆 + 三种进后台。

对主 Agent 始终是**同一个**工具、**同一份** schema（不随分流类型变化）：用 `type` 参数分流
定义式 / Fork 式两条路径。

  - type=definitional：取角色起**空白对话**跑 runner；权限模式 / 模型 / 最大轮次 / 白黑名单来自角色。
  - type=fork        ：**纯克隆**父 system / 历史 / 工具集，任务指令置于历史末尾（滚动缓存断点
                       之后，命中父 prompt cache 省钱）；**强制后台 + 强制 dontAsk + 不套角色**。

三种进后台（spec 能力 13）：① 入参显式 background；② 前台超时自动转（runner 跑超阈值则 detach，
返回「已转后台」句柄）；③ 手动切（TUI，见 T12）。前台同步返回 ToolResult；转/走后台返回句柄、
结果后续经系统提示后缀（T9）回传。

依赖经构造注入（仿 #0012 LoadSkillTool(store)）。``execute`` 是同步契约（registry 丢线程跑）；
经 BackgroundAgentManager 的同步桥（run_coroutine_threadsafe）把子 Agent 协程投递到主循环
（仿 #0008 MCP 适配层），前台 detach、后台真并行皆复用之。
"""

import concurrent.futures
import logging
import time

from coreagent.agents import constants
from coreagent.agents.runner import run_subagent
from coreagent.agents.runtime import build_subagent_runtime
from coreagent.conversation import Conversation
from coreagent.permissions.modes import DONT_ASK
from coreagent.tools.base import Tool, ToolResult
from coreagent.worktree.constants import WorktreeError

logger = logging.getLogger(__name__)


def _has_tool_use(msg: dict) -> bool:
    """该消息是否含 tool_use 块（list 内容）。"""
    content = msg.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )


def clean_fork_seed(messages: list[dict]) -> list[dict]:
    """克隆父历史给 Fork：丢弃末尾「含未配平 tool_use 的 assistant」消息。

    spawn 时父对话末条正是触发本次委派的 assistant（带悬空 tool_use、尚无 tool_result）。直接克隆
    会让 fork 历史以悬空 tool_use 结尾（API 要求 tool_use 紧跟 tool_result）→ 必报错。逐条 pop 至
    干净边界（末条为 user 文本或纯文本 assistant）。逐条浅拷贝，避免与父共享引用。
    """
    msgs = [dict(m) for m in messages]
    while msgs and msgs[-1].get("role") == "assistant" and _has_tool_use(msgs[-1]):
        msgs.pop()
    return msgs


class AgentTool(Tool):
    """统一委派工具（持子 Agent 子系统各依赖；对主 Agent schema 稳定）。"""

    name = constants.DELEGATION_TOOL_NAME
    description = (
        "把一个子任务委派给独立的子 Agent：子 Agent 有干净的上下文、受限工具集、独立权限追踪，"
        "跑到底后只把结果送回，中间过程不进主对话。type=definitional 用预定义角色从空白对话起；"
        "type=fork 继承当前对话历史与工具集（强制后台）。需要把"
        "「读一大片代码找答案 / 跑一轮专项审查 / 批量子任务」隔离出去、主对话只收结论时调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": [constants.TYPE_DEFINITIONAL, constants.TYPE_FORK],
                "description": "委派类型：definitional=角色式空白对话；fork=克隆当前对话（强制后台）",
            },
            "role": {
                "type": "string",
                "description": "definitional 必填：角色名（取自上下文中的可用角色目录）；fork 忽略",
            },
            "task": {
                "type": "string",
                "description": "委派给子 Agent 的任务指令（追加为子对话末条 user 消息）",
            },
            "background": {
                "type": "boolean",
                "description": "是否后台执行（缺省 false，前台阻塞返回结果）；fork 恒为后台",
            },
        },
        "required": ["type", "task"],
    }
    # 委派是一次有后果的动作：归写类（串行 + 走主 Agent 确认/规则门禁）；子 Agent 内部另有独立门禁。
    requires_confirmation = True

    def __init__(
        self,
        *,
        provider,
        config,
        registry,
        role_store,
        background_manager,
        conversation: Conversation,
        system_prompt: str | None,
        hooks=None,
        base_dir=None,
        worktree_manager=None,
        foreground_timeout: float = constants.FOREGROUND_TIMEOUT_SECONDS,
    ) -> None:
        self._provider = provider
        self._config = config
        self._registry = registry
        self._roles = role_store
        self._bg = background_manager
        self._conversation = conversation
        self._system_prompt = system_prompt
        self._hooks = hooks
        self._base_dir = base_dir
        # worktree 隔离子系统（#0015）：声明 isolation=worktree 的角色委派时建工作树；None = 未启用
        # （声明隔离的角色将硬失败，绝不静默回退共享工作区）。
        self._worktree_manager = worktree_manager
        self._foreground_timeout = foreground_timeout

    def bind(self, *, conversation=None, system_prompt=None, hooks=None) -> None:
        """晚绑定迟到的依赖（main 在 build_system_prompt / 载入会话 / 装配 hooks 后回填）。

        spawn_agent 须在 build_system_prompt **之前**注册（名字才进系统提示与工具列表），而
        conversation / system_prompt / hooks 在其后才就绪——故经本方法回填，不破坏注册时序。
        """
        if conversation is not None:
            self._conversation = conversation
        if system_prompt is not None:
            self._system_prompt = system_prompt
        if hooks is not None:
            self._hooks = hooks

    # ── 同步执行入口（registry 经 to_thread 调用；内部用同步桥投递协程到主循环）─────────
    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        # cwd（#0015）：委派工具本身不涉文件路径，忽略即可；子 Agent 的 cwd 由 build_subagent_runtime
        # 据角色 isolation 声明决定（隔离 = 工作树绝对路径，否则 = 项目根）。
        type_ = str(arguments.get("type", "")).strip()
        task = str(arguments.get("task", "")).strip()
        if not task:
            return ToolResult.fail("spawn_agent 失败：缺少任务指令 task")
        if type_ not in (constants.TYPE_DEFINITIONAL, constants.TYPE_FORK):
            return ToolResult.fail(
                f"spawn_agent 失败：未知 type「{type_}」，应为 "
                f"{constants.TYPE_DEFINITIONAL} / {constants.TYPE_FORK}"
            )

        agent_id = self._bg.next_id()
        explicit_bg = bool(arguments.get("background", False))

        if type_ == constants.TYPE_FORK:
            # Fork：纯克隆父历史（剔除悬空 tool_use）+ 强制后台 + 强制 dontAsk + 不套角色（无隔离）。
            seed = clean_fork_seed(self._conversation.get_messages())
            runtime = build_subagent_runtime(
                agent_id, provider=self._provider, config=self._config,
                registry=self._registry, role=None, permission_mode=DONT_ASK,
                background=True, hooks=self._hooks, seed_messages=seed, base_dir=self._base_dir,
                worktree_manager=self._worktree_manager,
            )
            system = self._system_prompt
            role_label, kind, background = "fork", constants.TYPE_FORK, True
        else:
            role_name = str(arguments.get("role", "")).strip()
            if not role_name:
                return ToolResult.fail("spawn_agent 失败：definitional 委派缺少角色名 role")
            role = self._roles.get(role_name) if self._roles is not None else None
            if role is None:
                known = "、".join(self._roles.names()) if self._roles is not None else ""
                return ToolResult.fail(
                    f"spawn_agent 失败：未知角色「{role_name}」。可用角色：{known or '（无）'}"
                )
            # 隔离工作区创建失败硬失败（#0015）：声明 isolation=worktree 但不在 git 仓 / 未启用 / 建失败
            # → 直接报错终止，绝不静默回退共享工作区（避免误以为已隔离）。
            try:
                runtime = build_subagent_runtime(
                    agent_id, provider=self._provider, config=self._config,
                    registry=self._registry, role=role, permission_mode=role.permission_mode,
                    background=explicit_bg, hooks=self._hooks, seed_messages=None,
                    base_dir=self._base_dir, worktree_manager=self._worktree_manager,
                )
            except WorktreeError as e:
                return ToolResult.fail(
                    f"spawn_agent 失败：角色「{role_name}」的隔离工作区创建失败（{e}）；"
                    f"已硬失败、未启动子 Agent。"
                )
            system = role.body
            role_label, kind, background = role.name, constants.TYPE_DEFINITIONAL, explicit_bg

        coro = run_subagent(
            runtime, provider=self._provider, registry=self._registry, system=system, task=task,
        )

        # 投递到主循环（真并行执行）。
        try:
            future = self._bg.schedule(coro)
        except RuntimeError as e:
            coro.close()
            return ToolResult.fail(f"spawn_agent 失败：子 Agent 执行不可用（{e}）")

        # ── 后台路径（显式 / Fork 强制）：登记 + 立即返回句柄；结果后续经系统后缀回传 ──
        if background:
            self._bg.track(future, agent_id=agent_id, role=role_label, kind=kind)
            return ToolResult.ok(
                f"已在后台启动子 Agent「{agent_id}」（角色={role_label}，类型={kind}）。"
                f"它与主对话真并行推进，完成后结果会经系统提示自动回传，无需等待。"
            )

        # ── 前台路径：阻塞至完成；超时自动转后台 / 用户手动切后台（轮询 detach 信号）──
        outcome, result = self._wait_foreground(future)
        if outcome != "done":
            # 超时（timeout）或手动切（detach）：detach，登记为后台任务、返回句柄、子 Agent 继续跑。
            self._bg.track(future, agent_id=agent_id, role=role_label, kind=kind)
            reason = (
                f"前台执行超过 {int(self._foreground_timeout)} 秒，已自动转入后台"
                if outcome == "timeout"
                else "已被手动切到后台"
            )
            return ToolResult.ok(
                f"子 Agent「{agent_id}」（角色={role_label}）{reason}继续；完成后结果经系统提示回传。"
            )
        if result is None:
            return ToolResult.fail(f"子 Agent「{agent_id}」已取消")

        # 完成：同步返回末轮摘要（前台阻塞式结果）。
        prefix = "" if result.ok else f"（注意：子 Agent 以错误 {result.error} 结束）\n"
        return ToolResult.ok(
            f"{prefix}子 Agent「{agent_id}」（角色={role_label}）执行完毕，结果：\n{result.summary}"
        )

    # 前台等待轮询步长（秒）：小步轮询使「手动切后台」能及时被响应，又不空转。
    _POLL_SLICE = 0.5

    def _wait_foreground(self, future: concurrent.futures.Future):
        """前台阻塞等待，小步轮询以兼顾「超时自动转」与「手动切后台」。

        返回 (outcome, result)：
          - ("done", SubAgentResult) 正常完成；
          - ("done", None)           被取消；
          - ("timeout", None)        超过前台阈值；
          - ("detach", None)         用户手动切后台（bg_manager.request_detach()）。
        """
        self._bg.arm_detach()
        deadline = time.monotonic() + self._foreground_timeout
        while True:
            if self._bg.detach_requested():
                return ("detach", None)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ("timeout", None)
            try:
                return ("done", future.result(timeout=min(remaining, self._POLL_SLICE)))
            except concurrent.futures.TimeoutError:
                continue
            except concurrent.futures.CancelledError:
                return ("done", None)
