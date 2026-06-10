"""多轮 Agent Loop（多步工具编排）。

一个用户输入内，模型可连环调用工具（读→改→验证）直到任务自然完成或触发边界：

  每轮：调模型（带工具流式）→ 解析响应 → 分类执行工具（读类并发 / 写类串行）
        → tool_result 回灌历史 → 检查四个终止条件 → 进入下一轮。

四个终止条件（每轮开始前 + 工具执行边界检查）：
  ① 模型本轮无工具调用（自然完成）；② 达到轮数上限；③ 收到取消信号；④ 不可恢复错误。

本函数是 async 生成器：把循环过程翻译为一组**单向类型化事件**（见 coreagent.events）依序 yield 给
消费者——流式文本（thinking / answer）、工具调用 / 结果、轮次开始、重试、运行级终止 / 错误。引擎与
渲染解耦：本层**绝不** import 渲染 / 终端库、**绝不** print。确认（confirm）/ 门禁（gate）是双向 /
控制型交互，表达不了单向 yield，仍为参数注入。assistant 与 tool_result 消息由本函数以追加方式写入
会话历史（可审计、可重放）。

设计：循环本体保持极简 while-loop，不引规划器 / 状态图 / LangGraph（最小脚手架，最大运行环境）。
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from enum import Enum

from coreagent.conversation import Conversation
from coreagent.events import (
    Retry,
    RunDone,
    RunEndReason,
    RunError,
    TextDelta,
    TextKind,
    ToolCall,
    ToolResultEvent,
    TurnStart,
)
from coreagent.hooks.models import (
    PERMISSION_DENIED,
    POST_TOOL_USE,
    POST_TOOL_USE_FAILURE,
    PRE_TOOL_USE,
)
from coreagent.injection import (
    build_hook_message,
    build_memory_message,
    build_reminder_message,
    build_skill_activation_message,
)
from coreagent.permissions.rules import ALLOW, ASK, DENY, Decision
from coreagent.providers.base import ChunkType
from coreagent.retry import stream_with_retry
from coreagent.tools.base import ToolResult
from coreagent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 用户拒绝执行工具时回灌给模型的文案（见 checklist 固定值）。
REJECTED_MESSAGE = "用户拒绝执行该工具"
# 取消时为「尚未产出真实结果」的 tool_use 补齐的占位文案（见 checklist 固定值）。
CANCELLED_MESSAGE = "已取消执行该工具"
# plan-only 模式下拦截写类后回灌的结构化结果文案（见 checklist 固定值）。
PLAN_RECORDED_MESSAGE = "plan-only 模式：已记录该操作，未执行"
# 授权门禁判 deny 时回灌给模型的文案前缀（#0007）。
DENIED_MESSAGE = "权限系统拒绝执行该工具"
# PreToolUse Hook 命中拦截码（exit 2）时回灌给模型的文案前缀（#0013；与 deny 回灌同格式）。
HOOK_BLOCKED_MESSAGE = "Hook 拦截执行该工具"
# 一个用户输入内模型调用次数上限（轮数上限）。
MAX_ROUNDS = 25
# 服务端搜索（#0024 web_search）pause_turn 续跑上限：防服务端工具多轮循环失控。
# 远小于 MAX_ROUNDS，故续跑不会被误判为达工具轮上限；超限则以自然完成收尾、不死循环。
MAX_PAUSE_TURNS = 5
# 某轮模型只产出工具调用 / 空文本时的占位答复，避免写入空 assistant 消息。
_EMPTY_REPLY_PLACEHOLDER = "（已完成工具调用）"
# 服务端 web_search 工具名（#0024）：映射事件 / 卡片呈现的统一标识。
WEB_SEARCH_TOOL = "web_search"


class ConfirmDecision(Enum):
    """执行前确认的结构化决策（#0007；supersedes #0004/#0005 的 bool 契约）。

    承载 HITL 三级粒度（once / session / persist）+ 拒绝。循环侧只据 ``.approved`` 决定是否
    执行；粒度副作用（会话信任集、persist 落盘）由确认回调自身（TUI）承担，循环不感知。
    """

    ONCE = "once"        # 本次允许
    SESSION = "session"  # 本会话允许（复用会话信任集）
    PERSIST = "persist"  # 永久写入规则文件（Local 作用域）
    REJECT = "reject"    # 拒绝 / esc / Ctrl+C

    @property
    def approved(self) -> bool:
        return self is not ConfirmDecision.REJECT


# 确认回调：返回结构化决策（不再是 bool）。
ConfirmCb = Callable[[dict], Awaitable[ConfirmDecision]]
# 授权门禁：(工具名, 入参) -> Decision（含 .outcome ∈ {allow/ask/deny}、.reason）。
GateCb = Callable[[str, dict], Decision]


def _deny_feedback(decision: Decision | None) -> str:
    """门禁判 deny 时回灌给模型的文案：带上理由 / 命中规则，便于模型调整。"""
    if decision is None:
        return DENIED_MESSAGE
    detail = decision.reason or (
        f"命中规则 {decision.matched_rule}" if decision.matched_rule else ""
    )
    return f"{DENIED_MESSAGE}：{detail}" if detail else DENIED_MESSAGE


def _result_block(tc: dict, content: str, is_error: bool) -> dict:
    """组装一条 tool_result（供 conversation.add_tool_results 回灌）。"""
    return {"tool_use_id": tc["id"], "content": content, "is_error": is_error}


def _server_tool_result_event(result: dict) -> ToolResultEvent:
    """把 provider 透出的服务端工具结果（web_search_tool_result）映射成既有 ``ToolResultEvent``。

    服务端工具**不经 ToolRegistry 执行**（服务端已执行）；这里仅把结果块翻译成既有事件供前端渲染：
    - 成功：content 序列化为来源列表（title + url）的 JSON 串，前端据此渲染「来源」卡片；
    - 失败（如 max_uses_exceeded）：转 ``ToolResult.fail``（is_error=True），供前端 / 历史区分降级。
    配对靠 ``tool_use_id``（== server_tool_use.id）；**不新增事件类型**（复用 #0018 ToolResultEvent）。
    """
    tool_use_id = result.get("tool_use_id", "")
    content = result.get("content")
    call = {"id": tool_use_id, "name": WEB_SEARCH_TOOL, "input": {}}
    if result.get("is_error"):
        code = content.get("error_code", "") if isinstance(content, dict) else ""
        return ToolResultEvent(call, ToolResult.fail(f"web_search 失败：{code}".rstrip("：")))
    sources = [
        {"title": r.get("title", ""), "url": r.get("url", "")}
        for r in (content or [])
        if isinstance(r, dict)
    ]
    return ToolResultEvent(call, ToolResult.ok(json.dumps(sources, ensure_ascii=False)))


def _format_plan_item(tc: dict) -> dict:
    """把被拦截的写类调用记成一条计划项（工具名 + 关键参数，格式化为一行）。"""
    try:
        args = json.dumps(tc.get("input", {}), ensure_ascii=False)
    except (TypeError, ValueError):
        args = str(tc.get("input", {}))
    return {"name": tc["name"], "input": tc.get("input", {}), "text": f"{tc['name']} {args}"}


def _rollback_uncommitted_user(conversation: Conversation) -> None:
    """回滚末条「纯文本 user 提问」——本回合尚未产出任何工具 / assistant 块时视作未提交。

    append-only：从不删既有（已配对）消息；仅当末条是字符串内容的 user 消息才回滚。
    """
    msgs = conversation.messages
    if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], str):
        msgs.pop()


def _gate_outcome(gate: GateCb | None, tc: dict, *, is_write: bool) -> tuple[Decision | None, str]:
    """取一次工具调用的门禁裁决；无 gate 时回落既有默认（读 allow / 写 ask）。

    返回 (decision, outcome)：decision 为门禁产出（无 gate 时 None），outcome ∈ {allow/ask/deny}。
    门禁自身异常 → fail-closed 判 deny（不静默放行、不冒泡中断本回合，历史仍可配平）。
    """
    if gate is None:
        return None, (ASK if is_write else ALLOW)
    try:
        decision = gate(tc["name"], tc["input"])
        return decision, decision.outcome
    except Exception as exc:  # noqa: BLE001 —— 门禁异常 fail-closed：拒绝，避免中断回合致历史失衡
        logger.warning("授权门禁异常，fail-closed 拒绝执行 %s：%r", tc.get("name"), exc)
        denied = Decision(
            DENY, stage="gate-error",
            reason=f"授权门禁异常（fail-closed 拒绝）：{type(exc).__name__}",
        )
        return denied, DENY


async def _confirm_decision(confirm: ConfirmCb | None, tc: dict) -> "ConfirmDecision":
    """调确认回调，缺省（无回调）按拒绝处理。"""
    if confirm is None:
        return ConfirmDecision.REJECT
    return await confirm(tc)


async def _run_tools(
    tool_calls: list[dict],
    registry: ToolRegistry,
    *,
    gate: GateCb | None,
    confirm: ConfirmCb | None,
    cancel,
    plan_only: bool,
    plan_items: list,
    collector: dict,
    hooks=None,
    cwd: str | None = None,
):
    """执行一轮工具调用：**读写均先过门禁**（deny 拦 / ask 确认 / allow 放行）；
    放行的读类并发、写类串行；plan 拦截写类；取消时补齐占位。

    本函数是 **async 生成器**：yield 工具调用事件（``ToolCall``，**调用序**）与工具结果事件
    （``ToolResultEvent``，读类**完成序** / 写类串行序，含失败 / 被拒）。最终把
    ``(results, cancelled)`` 写入 ``collector``（私有收尾通道，**不外泄**给消费者）：
      - ``collector["results"]`` 按模型**原调用顺序**排列（确定性、可重放）；
      - ``ToolResultEvent`` 的 emit 序按**完成先后**（实时进度，不要求与调用序一致）——
        两者顺序语义有意分开：历史要稳定、UI 要实时；
      - ``collector["cancelled"]`` 表示中途被取消（仍无真实结果的 tool_use 已补「已取消」占位）。

    门禁（gate）为工具执行的统一前置：读与写都先取 Decision。无 gate（如单测）时退回既有
    默认——读 allow（并发直跑）、写 ask（确认）——保证旧行为不变。
    """

    def cancelled_now() -> bool:
        return cancel is not None and cancel.is_set()

    results: list[dict | None] = [None] * len(tool_calls)

    def _store_denied(idx: int, tc: dict, decision: Decision | None) -> ToolResult:
        """门禁 deny：存 is_error 结果块，返回被拒 ToolResult（供生成器 yield 结果事件）。"""
        blocked = ToolResult.fail(_deny_feedback(decision))
        results[idx] = _result_block(tc, blocked.content, True)
        return blocked

    # ── Hook 挂点（#0013 T5）：无 hooks 时全为 no-op；任何失败已在 HookManager 内隔离。──
    async def _fire_pre(tc: dict):
        """PreToolUse：在门禁之前触发；命中拦截码返回带 blocked 的 outcome，否则放行。"""
        if hooks is None:
            return None
        return await hooks.fire(PRE_TOOL_USE, tool_name=tc["name"], tool_input=tc["input"])

    def _store_hook_blocked(idx: int, tc: dict, reason: str) -> ToolResult:
        """PreToolUse 硬拦：把拒绝理由当 is_error 工具结果（与 deny 同格式、同路径）。"""
        detail = f"{HOOK_BLOCKED_MESSAGE}：{reason}" if reason else HOOK_BLOCKED_MESSAGE
        blocked = ToolResult.fail(detail)
        results[idx] = _result_block(tc, blocked.content, True)
        return blocked

    async def _fire_post(tc: dict, result: ToolResult) -> None:
        """工具完成后按成功 / 失败触发 PostToolUse / PostToolUseFailure（观测 / 可注入）。"""
        if hooks is None:
            return
        event = POST_TOOL_USE if result.success else POST_TOOL_USE_FAILURE
        await hooks.fire(
            event, tool_name=tc["name"], tool_input=tc["input"],
            extra={"success": result.success, "result": result.content},
        )

    async def _fire_denied(tc: dict, decision: Decision | None) -> None:
        """门禁判 deny 时触发 PermissionDenied（观测 / 可注入；本期不提供「让模型重试」返回）。"""
        if hooks is None:
            return
        await hooks.fire(
            PERMISSION_DENIED, tool_name=tc["name"], tool_input=tc["input"],
            extra={"reason": _deny_feedback(decision),
                   "matched_rule": getattr(decision, "matched_rule", None)},
        )

    # 按 requires_confirmation 切读 / 写两组（保留各自原索引）。读 = 免确认（只读），
    # 写 = 需确认（有副作用）；本期直接复用「需确认」标记当读 / 写判据（最简方案优先）。
    reads: list[tuple[int, dict]] = []
    writes: list[tuple[int, dict]] = []
    for idx, tc in enumerate(tool_calls):
        tool = registry.get_optional(tc["name"])
        if tool is not None and tool.requires_confirmation:
            writes.append((idx, tc))
        else:
            reads.append((idx, tc))

    # ── 读类：逐个先过门（调用序）→ deny 拦 / ask 确认 / allow 收集；放行的并发执行 ──
    #    tool_result 按原索引回灌（调用序）、结果事件按完成序 yield。
    if reads and not cancelled_now():
        runnable: list[tuple[int, dict]] = []  # 过门后可并发执行的读类
        for idx, tc in reads:
            if cancelled_now():
                break
            yield ToolCall(tc)  # 调用序 emit
            # PreToolUse 在门禁**之前**：命中拦截码 → 跳过门禁与执行，理由回灌。
            pre = await _fire_pre(tc)
            if pre is not None and pre.blocked:
                yield ToolResultEvent(tc, _store_hook_blocked(idx, tc, pre.reason), rejected=True)
                continue
            decision, outcome = _gate_outcome(gate, tc, is_write=False)
            if outcome == DENY:
                await _fire_denied(tc, decision)
                yield ToolResultEvent(tc, _store_denied(idx, tc, decision), rejected=True)
            elif outcome == ASK:
                # 罕见：读被规则置为 ask（默认预置 Read(*) 放行，故通常不触发）。
                d = await _confirm_decision(confirm, tc)
                if cancelled_now():
                    break
                if not d.approved:
                    rejected = ToolResult.fail(REJECTED_MESSAGE)
                    results[idx] = _result_block(tc, REJECTED_MESSAGE, True)
                    yield ToolResultEvent(tc, rejected, rejected=True)
                else:
                    runnable.append((idx, tc))
            else:  # ALLOW
                runnable.append((idx, tc))

        if runnable and not cancelled_now():
            async def _run_read(idx: int, tc: dict) -> tuple[int, dict, ToolResult]:
                result = await registry.execute(tc["name"], tc["input"], cwd=cwd, cancel=cancel)
                return idx, tc, result

            # as_completed 按**完成序**产出 → 结果事件按完成序 yield（哪个先跑完哪个先反馈）；
            # tool_result 仍按原索引回灌（调用序）。两序有意分开。
            tasks = [asyncio.ensure_future(_run_read(i, t)) for i, t in runnable]
            for fut in asyncio.as_completed(tasks):
                idx, tc, result = await fut
                yield ToolResultEvent(tc, result, rejected=False)
                await _fire_post(tc, result)
                results[idx] = _result_block(tool_calls[idx], result.content, not result.success)

    # ── 写类串行：plan 拦截 / 过门（deny 拦 / allow 直放 / ask 确认）/ 执行；取消后余下补占位 ──
    for idx, tc in writes:
        if cancelled_now():
            break
        yield ToolCall(tc)

        # PreToolUse 在门禁 / plan 拦截**之前**：命中拦截码 → 跳过后续、理由回灌。
        pre = await _fire_pre(tc)
        if pre is not None and pre.blocked:
            yield ToolResultEvent(tc, _store_hook_blocked(idx, tc, pre.reason), rejected=True)
            continue

        if plan_only:
            # plan-only：不执行、不过门、不 confirm，记一条计划项，回灌「已记录未执行」结构化结果。
            plan_items.append(_format_plan_item(tc))
            recorded = ToolResult.ok(PLAN_RECORDED_MESSAGE)
            results[idx] = _result_block(tc, PLAN_RECORDED_MESSAGE, False)
            yield ToolResultEvent(tc, recorded, rejected=False)
            continue

        decision, outcome = _gate_outcome(gate, tc, is_write=True)
        if outcome == DENY:
            await _fire_denied(tc, decision)
            yield ToolResultEvent(tc, _store_denied(idx, tc, decision), rejected=True)
            continue
        if outcome == ASK:
            d = await _confirm_decision(confirm, tc)
            if cancelled_now():
                # 确认期间被取消：该写类「已发起但被中断未完成」→ 留待补占位。
                break
            if not d.approved:
                rejected = ToolResult.fail(REJECTED_MESSAGE)
                results[idx] = _result_block(tc, REJECTED_MESSAGE, True)
                yield ToolResultEvent(tc, rejected, rejected=True)
                continue
        # outcome == ALLOW（含 acceptEdits 自动批准）或 ask 已获批 → 执行。

        result = await registry.execute(tc["name"], tc["input"], cwd=cwd, cancel=cancel)
        yield ToolResultEvent(tc, result, rejected=False)
        await _fire_post(tc, result)
        results[idx] = _result_block(tc, result.content, not result.success)

    # ── 取消补齐：仍无真实结果的 tool_use 一律补「已取消」占位（配平、可重放）；
    #    已完成的（含正在执行、等其跑完的廉价读类）保留真实结果，不替换。 ──
    cancelled = cancelled_now()
    if cancelled:
        for idx, tc in enumerate(tool_calls):
            if results[idx] is None:
                results[idx] = _result_block(tc, CANCELLED_MESSAGE, True)

    # 非取消路径下每个 tool_use 必有真实结果（读全 await、写全处理）；不留 None。
    collector["results"] = [
        r if r is not None else _result_block(tool_calls[i], CANCELLED_MESSAGE, True)
        for i, r in enumerate(results)
    ]
    collector["cancelled"] = cancelled


async def run_agent_turn(
    provider,
    conversation: Conversation,
    registry: ToolRegistry,
    system: str | None = None,
    *,
    env_block: str | None = None,
    gate: GateCb | None = None,
    confirm: ConfirmCb | None = None,
    cancel=None,
    plan_only: bool = False,
    context=None,
    instructions_block: str | None = None,
    memory_block: str | None = None,
    skills_catalog_block: str | None = None,
    skills=None,
    allow_tools: set[str] | None = None,
    model_override: str | None = None,
    cwd: str | None = None,
    hooks=None,
    max_rounds: int = MAX_ROUNDS,
    background_results_block: str | None = None,
    team_inbox_block: str | None = None,
    extra_tool_filter: set[str] | None = None,
    server_tools: list[dict] | None = None,
) -> "AsyncGenerator":
    """多轮 Agent Loop：调模型 → 分类执行工具 → 回灌 → 检查终止条件，直到自然完成 / 上限 / 取消 / 错误。

    yield 一组单向类型化事件（见 ``coreagent.events``）：``TurnStart`` / ``TextDelta``（thinking /
    answer）/ ``ToolCall`` / ``ToolResultEvent`` / ``Retry`` / ``RunDone`` / ``RunError``。

    - ``cancel``：可选取消令牌（任何带 ``is_set()`` 的对象，如 ``asyncio.Event``），每轮开始与每个
      工具执行前检查；取消后优雅补齐 tool_result、emit 循环结束（原因 = 取消）。
    - ``gate``：授权门禁（#0007）。每个工具（含读）执行前先取 Decision——deny 拦、ask 走确认、
      allow 放行。缺省 None 时退回既有默认（读放行 / 写确认），保证旧调用行为不变。
    - ``plan_only``：plan-only 开关。读类照常执行，写类被拦截记为计划项；计划列表随循环结束信号交还。
    - ``env_block``：会话启动时快照的环境块（带 <env> 标签）；作为 system 尾部块（不进可缓存前缀）注入，
      **不写入** conversation。缺省 None 时不注入（与既有调用行为一致）。
    - ``context``：上下文管理器（#0009）。每轮调模型前调其「每请求前」入口（先卸载、后判摘要），
      DONE 后把服务端 usage 回写为 token 锚点。缺省 None 时全程 no-op（旧调用/旧测试行为不变）。
    - ``instructions_block``：项目指令文件块（带 <project-instructions> 标签，#0010）。作为 system
      尾部块注入（其项目/本地层随 cwd 变，不进可缓存前缀）、**不写入** conversation。缺省 None 不注入。
    - ``memory_block``：记忆上下文（长期记忆索引 + 上次会话恢复摘要，#0010）。**仅首轮**（round 1）
      经动态注入通道（滚动断点之后、不打缓存、不写历史）注入一次，相当于 Agent 启动即「已读过」。
    - ``skills_catalog_block``：技能启动目录块（名字 + 一句话说明，#0012）。作为 system 尾部块注入
      （含 <available-skills> 标签、不进可缓存前缀），模型据此按需调 load_skill 激活。缺省不注入。
    - ``skills``：技能仓（SkillStore，#0012）。非空时**每轮**按当前激活集重算工具列表（白名单并集
      裁剪、loader 恒在）并注入「激活指令块」（每轮重建、走动态通道）。缺省 None 时不参与（旧行为）。
    - ``allow_tools``：固定工具白名单（独立模式子对话用，#0012）。仅当 ``skills`` 为空时生效：
      工具列表恒为 registry 按此集合裁剪的结果。缺省 None → 全量工具。
    - ``model_override``：本回合改用的模型（独立模式技能专属模型，#0012）；为空走 config.model。
    - ``cwd``：本回合所有工具执行的工作目录（#0015），原样透传到每次 ``registry.execute``。
      主 Agent = 项目根；隔离子 Agent = 其工作树绝对路径。缺省 None → 各工具退回进程 cwd（旧行为不变）。
      **按调用透传**、不落工具实例态——并行子 Agent 共享工具实例，实例态会串味。
    - ``hooks``：生命周期 Hook 管理器（#0013）或子 Agent 的 Hook 运行时（#0014）。非空时：工具
      执行边界触发 PreToolUse（拦截在门禁前）/ PostToolUse / PostToolUseFailure / PermissionDenied；
      每轮请求组装前把汇总的注入文本经动态通道（滚动断点之后、不打缓存、不写历史）注入一次。缺省
      None 全程 no-op（旧行为）。子 Agent 传 per-agent 运行时（#0014 T5），可变态与主会话隔离。
    - ``max_rounds``：一个用户输入内的轮数上限（#0014 T7）。缺省 ``MAX_ROUNDS``（不传时行为不变）；
      子 Agent 据角色 ``max-turns`` 透传，达上限以 RunEndReason.MAX_TURNS 退出。
    - ``background_results_block``：后台子 Agent 结果系统提示后缀块（#0014 T9，含
      <background-agent-results> 标签）。非空时作为 system **尾部块**（不进可缓存前缀）追加一次；
      其内容由上层在装配前 drain（消费即清）得到，故只在「下一轮请求」出现、读完即清、不重复注入。
      缺省 None 不追加（旧行为）。
    - ``team_inbox_block``：团队邮箱注入后缀块（#0016 T8，含 <team-inbox> 标签）。与
      ``background_results_block`` 同范式：作为 system **尾部块**（不进可缓存前缀）追加一次；来源由
      上层 drain（消费即清），故只在下一轮请求出现、读完即清、不重复注入；不走 Hook 注入旁路（与
      ``_pending`` 不争用）。缺省 None 不追加（旧行为）。
    - ``extra_tool_filter``：附加工具白名单（#0016 T12 delegate 模式）。非空时与既有过滤（skills /
      allow_tools / 全量）**取交**——正交叠加、不进权限阶梯；用于 delegate 开启时把 Lead 工具集收窄到
      「读类 + shell + 团队工具」（去掉写/编辑类）。缺省 None 不收窄（旧行为）。
    - ``server_tools``：服务端工具声明列表（#0024 web_search，每项 ``{"type","name",...}`` **无**
      input_schema）。非空时追加到工具清单**尾部**（不进 registry 裁剪——服务端工具非本仓登记、由端点
      执行）；provider 透传给端点，端点回传的服务端工具块经 #0018 既有事件映射给前端。缺省 None 不追加。
    """
    # 稳定模块块（system）+ 项目指令块 + 环境快照块 + 技能目录块：装配为结构化 system 交 provider
    # 分通道缓存；后三者 session-specific、不写历史、不进可缓存前缀。全为空时退回纯字符串（向后兼容）。
    # 后台结果后缀块（#0014 T9）置于尾部：含 <background-agent-results> 标签，provider 据此不打
    # cache_control（不进可缓存前缀，同 <env> / <available-skills>）。来源已由上层 drain（消费即清），
    # 故只在本次（=下一轮）请求出现、读完即清、不重复注入；不走 _pending（与 Hook 注入旁路互不争用）。
    # 团队邮箱后缀块（#0016 T8）与后台结果后缀块同置尾部：含 <team-inbox> 标签，provider 据此不打
    # cache_control（不进可缓存前缀）；来源已由上层 drain（消费即清），只在本次请求出现、读完即清。
    _sys_parts = [
        p
        for p in (system, instructions_block, env_block, skills_catalog_block,
                  background_results_block, team_inbox_block)
        if p
    ]
    request_system: str | list | None = (
        _sys_parts if len(_sys_parts) > 1 else (_sys_parts[0] if _sys_parts else None)
    )
    plan_items: list = []
    committed = False  # 本回合是否已写入 assistant / tool_result（决定取消 / 错误时是否回滚 user 提问）
    round_index = 0
    pause_turns = 0  # 服务端搜索（#0024）pause_turn 续跑计数：上限 MAX_PAUSE_TURNS，防失控
    # 累计服务端 token 用量（各轮 DONE 之和）：随终止事件 RunDone 一次性交还消费者（headless 记账）。
    usage_in_total = 0
    usage_out_total = 0

    def loop_done(reason: RunEndReason) -> RunDone:
        return RunDone(
            reason=reason,
            plan=list(plan_items) if plan_only else None,
            input_tokens=usage_in_total,
            output_tokens=usage_out_total,
        )

    while True:
        # 终止条件①：取消（每轮开始检查）。
        if cancel is not None and cancel.is_set():
            if not committed:
                _rollback_uncommitted_user(conversation)
            yield loop_done(RunEndReason.CANCELLED)
            return

        # 终止条件②：达轮数上限——不再调模型，emit 运行级终止（原因 = 上限）。
        # 上限可配（#0014 T7）：缺省 MAX_ROUNDS；子 Agent 据角色 max-turns 透传。
        if round_index >= max_rounds:
            yield loop_done(RunEndReason.MAX_TURNS)
            return

        round_index += 1
        yield TurnStart(round_index=round_index)

        # 上下文管理（#0009）：每请求前先跑第一层卸载、再按估算判第二层摘要（就地改写历史）。
        # 任一环节异常已在管理器内降级兜底，不破坏历史配平。
        if context is not None:
            await context.before_request(conversation)

        # ── 调模型（带工具流式）；不可恢复错误（退避耗尽后上抛）→ 运行级错误事件并终止 ──
        text_parts: list[str] = []
        blocks: list | None = None
        tool_calls: list | None = None
        # 服务端工具（#0024 web_search）：DONE 透出 server_tool_use / web_search_tool_result + stop_reason，
        # 供映射到既有事件并据 pause_turn 续跑（普通对话三者均为 None / end_turn，旧路径不变）。
        server_tool_calls: list | None = None
        server_tool_results: list | None = None
        stop_reason: str | None = None
        # 动态注入：按（plan_only、轮序）产出临时提醒，追加到滚动断点之后的 messages 尾部；
        # 每请求重建、**不写入** conversation（历史仍 append-only 可重放）。
        request_messages = conversation.get_messages()
        reminder = build_reminder_message(plan_only=plan_only, round_index=round_index)
        if reminder is not None:
            request_messages = request_messages + [reminder]
        # 记忆上下文仅首轮注入一次（启动即「已读过」）：同走动态通道、不打缓存、不写历史。
        if round_index == 1:
            mem_msg = build_memory_message(memory_block)
            if mem_msg is not None:
                request_messages = request_messages + [mem_msg]
        # 技能激活指令块（#0012）：**每轮重建**（区别于记忆块仅首轮），按激活顺序叠加、占位符已替换；
        # 走动态通道（含 <system-reminder>，不打缓存、不写历史）、追加到滚动断点之后的尾部。
        if skills is not None:
            skill_msg = build_skill_activation_message(skills.activation_block())
            if skill_msg is not None:
                request_messages = request_messages + [skill_msg]
        # Hook 旁路注入（#0013 T8）：把 HookManager 累积的注入文本（prompt 正文 / 命令 stdout）
        # 经动态通道（滚动断点之后、含 <system-reminder>、不打缓存、不写历史）注入到本次请求；
        # 取走即清空（一次性消费），超长存文件、正文替换为预览 + 路径。
        if hooks is not None:
            hook_msg = build_hook_message(hooks.take_injection())
            if hook_msg is not None:
                request_messages = request_messages + [hook_msg]

        # 工具列表**每轮重算**（#0012）：有技能仓 → 按当前激活白名单并集裁剪（loader 恒在、无激活
        # 技能则全量）；否则有固定白名单（独立模式）→ 按之裁剪；都没有 → 全量（旧行为）。
        if skills is not None:
            allow_set: set[str] | None = skills.active_tool_filter()
        elif allow_tools is not None:
            allow_set = set(allow_tools)
        else:
            allow_set = None
        # delegate 协调模式工具收窄（#0016 T12）：与既有过滤**取交**（正交叠加，不进权限阶梯）。
        # None = 不收窄（旧行为）；非空 → 把 Lead 工具集裁到「读类 + shell + 团队工具」。
        if extra_tool_filter is not None:
            base = allow_set if allow_set is not None else set(registry.names())
            allow_set = set(base) & set(extra_tool_filter)
        tools_api = registry.to_api_tools(allow=allow_set)
        # 服务端工具声明（#0024 web_search）：追加到工具清单尾部——非本仓 ToolRegistry 登记、不进
        # allow_set 裁剪，由 provider 透传给端点（端点执行搜索、回传服务端工具块）。
        if server_tools:
            tools_api = tools_api + list(server_tools)

        # 流式消费循环（#0021）：把生成器握成变量，便于流式中途取消时显式 aclose 上游模型流。
        stream = stream_with_retry(
            provider,
            request_messages,
            system=request_system,
            tools=tools_api,
            model_override=model_override,
        )
        cancelled_mid_stream = False
        try:
            # stream_with_retry 产出 provider StreamChunk 与 Retry 事件的混流：
            # Retry → 原样透传给消费者；StreamChunk → 翻译为流式文本事件并在内部做 token 记账。
            async for item in stream:
                if isinstance(item, Retry):
                    yield item
                    continue
                chunk = item
                if chunk.type == ChunkType.THINKING:
                    yield TextDelta(TextKind.THINKING, chunk.content)
                elif chunk.type == ChunkType.TEXT:
                    text_parts.append(chunk.content)
                    yield TextDelta(TextKind.ANSWER, chunk.content)
                elif chunk.type == ChunkType.DONE:
                    blocks = chunk.blocks
                    tool_calls = chunk.tool_calls
                    server_tool_calls = chunk.server_tool_calls
                    server_tool_results = chunk.server_tool_results
                    stop_reason = chunk.stop_reason
                    # 累计服务端 usage（供终止事件一次性交还 headless 消费者记账）。
                    usage_in_total += chunk.input_tokens or 0
                    usage_out_total += chunk.output_tokens or 0
                    # 回写 token 锚点（#0009）：服务端输入计数（含缓存命中/创建）+ 当前已发送消息条数。
                    # 此刻 assistant 尚未追加，conversation 即本次请求所发消息，长度即锚点序号。
                    if context is not None and chunk.input_tokens is not None:
                        total_input = (
                            chunk.input_tokens
                            + (chunk.cache_read_input_tokens or 0)
                            + (chunk.cache_creation_input_tokens or 0)
                        )
                        context.record_usage(total_input, len(conversation.get_messages()))
                # 终止条件③′（#0021）：流式**中途**取消——每个增量后查令牌（不止轮次开始那处），
                # 命中即跳出消费循环，让 chat（单流无工具）与 code 都能在出字途中真正中断。
                if cancel is not None and cancel.is_set():
                    cancelled_mid_stream = True
                    break
        except Exception as exc:  # noqa: BLE001 —— 退避耗尽后的不可恢复错误（区别于单个工具失败）
            logger.warning("模型调用不可恢复失败：%r", exc)
            if not committed:
                _rollback_uncommitted_user(conversation)
            yield RunError(error_type=type(exc).__name__)
            return

        if cancelled_mid_stream:
            # 关闭上游模型流（GeneratorExit 传播到 provider.stream_chat，不在后台续烧 token）；
            # 未提交的本轮文本一律丢弃（不写半个 assistant），未提交则回滚 user 提问→历史配平。
            await stream.aclose()
            if not committed:
                _rollback_uncommitted_user(conversation)
            yield loop_done(RunEndReason.CANCELLED)
            return

        text = "".join(text_parts)

        # 服务端工具（#0024 web_search）：把 server_tool_use / web_search_tool_result 映射到**既有**事件
        # （ToolCall / ToolResultEvent），供前端走既有工具卡片分派——**不经 ToolRegistry 执行**（服务端
        # 已执行）、**不新增事件类型**。先发调用、再发结果（前端按 id 配对到同一卡片）。
        if server_tool_calls:
            for stc in server_tool_calls:
                yield ToolCall(stc)
        if server_tool_results:
            for r in server_tool_results:
                yield _server_tool_result_event(r)

        # 终止条件③：本轮无**客户端**工具调用。
        if not tool_calls:
            # 服务端搜索多轮（#0024 T6）：stop_reason==pause_turn → 记录本轮 assistant（含服务端工具块）
            # 后**自动续跑**（不加额外用户消息）、设续跑上限防失控；上限内 end_turn / 无 pause → 自然完成。
            if stop_reason == "pause_turn" and pause_turns < MAX_PAUSE_TURNS:
                if blocks is not None:
                    conversation.add_assistant_blocks(blocks)
                else:
                    conversation.add_assistant(text or _EMPTY_REPLY_PLACEHOLDER)
                committed = True
                pause_turns += 1
                continue
            # 自然完成（与 #0003 纯对话 / 单轮路径产出一致）；服务端工具块随 blocks 一并入历史。
            if blocks is not None:
                conversation.add_assistant(text, blocks=blocks)
            else:
                conversation.add_assistant(text or _EMPTY_REPLY_PLACEHOLDER)
            committed = True
            yield loop_done(RunEndReason.COMPLETE)
            return

        # 有客户端工具调用：记录含 tool_use（及服务端工具块，若同轮）的 assistant（供回灌多轮）。
        if blocks is not None:
            conversation.add_assistant_blocks(blocks)
        else:
            conversation.add_assistant(text)
        committed = True

        # 分类执行（读并发 / 写串行 / plan 拦截 / 取消补齐）：_run_tools 是 async 生成器，
        # yield 工具调用 / 结果事件（原样透传给消费者），(results, cancelled) 经 collector 私有回收。
        collector: dict = {}
        async for ev in _run_tools(
            tool_calls,
            registry,
            gate=gate,
            confirm=confirm,
            cancel=cancel,
            plan_only=plan_only,
            plan_items=plan_items,
            collector=collector,
            hooks=hooks,
            cwd=cwd,
        ):
            yield ev
        results, cancelled = collector["results"], collector["cancelled"]
        conversation.add_tool_results(results)

        # 终止条件④：执行中途被取消 → 优雅收尾（历史已配平 tool_use / tool_result）。
        if cancelled:
            yield loop_done(RunEndReason.CANCELLED)
            return

        # 否则进入下一轮（回灌后再调模型）。
