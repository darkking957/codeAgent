"""HookManager：事件分发 + 条件门 + 结果汇总 + 注入缓冲（#0013 T4；#0014 T5 加 per-agent 命名空间）。

对外暴露统一入口 ``fire(event, ...)``：按事件名取规则 → 条件求值（仅工具事件）→ 执行动作 → 汇总
「是否拦截 + 拦截理由」与「注入文本」。

两类事件语义：
  - 同步决策型（PreToolUse / PreCompact）：调用方 ``await fire(...)`` 后读 ``.blocked``/``.reason``
    决定是否拦截；首个硬拦立即短路（后续规则不再跑）。
  - 异步观测型（其余）：调用方仍 await（让同步动作的注入产出落入缓冲），但规则若标 ``async``
    则 fire-and-forget（不等其完成、不参与注入）。

**per-agent 命名空间（#0014 T5）**：已编译规则集（不可变）由 HookManager 共享持有；可变运行态
（``_pending`` 注入缓冲、``_once_fired`` only-once 集）抽到 ``HookRuntime``。主会话用一个**默认
运行时**（``manager.fire``/``take_injection`` 即走它，保证现行为与现有测试不变）；每个子 Agent
经 ``manager.new_runtime()`` 取独立运行时，``fire``/``take_injection`` 按运行时隔离，真并行下互不
串味、无需全局锁。
"""

import asyncio
import logging

from coreagent.hooks.actions import execute_action
from coreagent.hooks.models import Hook, is_blocking_event, is_tool_event
from coreagent.hooks.payload import build_payload

logger = logging.getLogger(__name__)


class HookOutcome:
    """一次 ``fire`` 的汇总结果（同步事件读 blocked/reason；注入已入缓冲，单独 take）。"""

    __slots__ = ("blocked", "reason", "inject")

    def __init__(self, blocked: bool = False, reason: str = "", inject: str = "") -> None:
        self.blocked = blocked
        self.reason = reason
        self.inject = inject


class HookRuntime:
    """per-agent 可变运行态（#0014 T5）：only-once 已触发集 + 注入缓冲。

    规则集仍由所属 HookManager 共享持有；本对象只隔离「会因触发而改变」的状态。``fire`` /
    ``take_injection`` 委派回 manager 但绑定本运行时，故同一 manager 下不同运行时的拦截计数与注入
    缓冲彼此独立（子 Agent 互不污染、与主会话互不串味）。
    """

    __slots__ = ("_manager", "_once_fired", "_pending")

    def __init__(self, manager: "HookManager") -> None:
        self._manager = manager
        # only-once 已触发集合（会话内内存标记，按规则对象 id；不跨会话持久化，见 #0013 Out of Scope）。
        self._once_fired: set[int] = set()
        # 待注入缓冲：观测/放行路径产出的注入文本，等调用方 take 走。
        self._pending: list[str] = []

    def take_injection(self) -> str | None:
        """取走并清空本运行时的待注入缓冲（多条以空行拼接）；无则 None。每次请求前调用一次。"""
        if not self._pending:
            return None
        text = "\n\n".join(p for p in self._pending if p.strip())
        self._pending.clear()
        return text or None

    async def fire(
        self,
        event: str,
        *,
        tool_name: str | None = None,
        tool_input: dict | None = None,
        extra: dict | None = None,
    ) -> HookOutcome:
        """在本运行时命名空间内触发某事件（委派回 manager 的共享规则集）。"""
        return await self._manager._fire(
            event, self, tool_name=tool_name, tool_input=tool_input, extra=extra
        )


class HookManager:
    """持有已编译规则列表（共享、不可变），按事件分发执行。可变运行态隔离在 HookRuntime。"""

    def __init__(self, hooks: list[Hook]) -> None:
        self._by_event: dict[str, list[Hook]] = {}
        for h in hooks:
            self._by_event.setdefault(h.event, []).append(h)
        # 主会话默认运行时：manager.fire / take_injection 即走它，保证现行为与现有测试不变。
        self._default = HookRuntime(self)

    @property
    def hook_count(self) -> int:
        return sum(len(v) for v in self._by_event.values())

    def new_runtime(self) -> HookRuntime:
        """为一个子 Agent 创建独立 Hook 运行时（共享规则、隔离 _pending/_once_fired）。"""
        return HookRuntime(self)

    # ── 主会话兼容入口：委派给默认运行时（#0013 既有调用 / 测试不变）─────────────────
    def take_injection(self) -> str | None:
        """取走并清空主会话默认运行时的注入缓冲；无则 None。供主循环每次请求前调用一次。"""
        return self._default.take_injection()

    async def fire(
        self,
        event: str,
        *,
        tool_name: str | None = None,
        tool_input: dict | None = None,
        extra: dict | None = None,
    ) -> HookOutcome:
        """在主会话默认运行时触发某事件（向后兼容）。子 Agent 应改用 new_runtime().fire(...)。"""
        return await self._fire(
            event, self._default, tool_name=tool_name, tool_input=tool_input, extra=extra
        )

    def _should_run(self, hook: Hook, event: str, tool_name: str | None,
                    tool_input: dict | None) -> bool:
        """条件门：无 `if` → 跑；有 `if` 但非工具事件 → 静默跳过；工具事件 → 按匹配结果。"""
        if hook.condition is None:
            return True
        if not is_tool_event(event):
            # 非工具事件上的 `if` 被静默忽略（该规则不运行），不报错、不警告。
            return False
        try:
            return hook.condition.matches(tool_name or "", tool_input or {})
        except Exception as e:  # noqa: BLE001 —— 条件求值异常：安全跳过（不运行该规则），不中断
            logger.warning("Hook 条件求值异常，跳过该规则：%r（event=%s）", e, event)
            return False

    async def _fire(
        self,
        event: str,
        runtime: HookRuntime,
        *,
        tool_name: str | None = None,
        tool_input: dict | None = None,
        extra: dict | None = None,
    ) -> HookOutcome:
        """在给定运行时命名空间触发某事件的全部规则；返回汇总结果。绝不上抛（动作层 + 此处兜底）。"""
        hooks = self._by_event.get(event)
        if not hooks:
            return HookOutcome()

        payload = build_payload(event, tool_name=tool_name, tool_input=tool_input, extra=extra)
        is_blocking = is_blocking_event(event)
        injects: list[str] = []

        for hook in hooks:
            if not self._should_run(hook, event, tool_name, tool_input):
                continue
            # only-once：会话内只触发一次（条件已满足才计数）；按运行时隔离。
            if hook.only_once:
                if id(hook) in runtime._once_fired:
                    continue
                runtime._once_fired.add(id(hook))
            # 异步观测型：fire-and-forget，不等其完成、不参与注入。
            if hook.async_:
                self._schedule_async(hook, payload)
                continue
            # 同步动作：丢到线程跑（仿 registry.execute），避免命令 / HTTP 阻塞整个事件循环；
            # 仍 await 结果——「同步等待」语义不变（拦截裁决必须等齐）。
            res = await asyncio.to_thread(
                execute_action, hook.action, payload,
                is_blocking=is_blocking, timeout=hook.timeout,
            )
            if is_blocking and res.blocked:
                # 首个硬拦立即短路：后续规则不再跑（拦截裁决确定）。
                return HookOutcome(blocked=True, reason=res.reason)
            if res.inject:
                injects.append(res.inject)

        if injects:
            runtime._pending.extend(injects)
        return HookOutcome(inject="\n\n".join(injects))

    def _schedule_async(self, hook: Hook, payload: dict) -> None:
        """把异步动作丢到后台任务跑（不阻塞主流程）；无运行中的事件循环时退回同步执行。"""
        async def _runner() -> None:
            await asyncio.to_thread(
                execute_action, hook.action, payload,
                is_blocking=False, timeout=hook.timeout,
            )

        try:
            asyncio.get_running_loop().create_task(_runner())
        except RuntimeError:
            # 无运行 loop（罕见，如同步测试上下文）：直接同步跑一次，仍不上抛。
            execute_action(hook.action, payload, is_blocking=False, timeout=hook.timeout)
