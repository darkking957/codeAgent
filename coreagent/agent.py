"""多轮 Agent Loop（多步工具编排）。

一个用户输入内，模型可连环调用工具（读→改→验证）直到任务自然完成或触发边界：

  每轮：调模型（带工具流式）→ 解析响应 → 分类执行工具（读类并发 / 写类串行）
        → tool_result 回灌历史 → 检查四个终止条件 → 进入下一轮。

四个终止条件（每轮开始前 + 工具执行边界检查）：
  ① 模型本轮无工具调用（自然完成）；② 达到轮数上限；③ 收到取消信号；④ 不可恢复错误。

本函数是 async 生成器：把每轮流式 chunk 依序 yield 给调用方渲染；另推送四个循环级信号
（轮开始 / 轮结束 / 循环结束 / 循环错误）让上层感知循环边界。确认 / 工具渲染 / 重试经回调
注入，与 TUI 解耦。assistant 与 tool_result 消息由本函数以追加方式写入会话历史（可审计、可重放）。

设计：循环本体保持极简 while-loop，不引规划器 / 状态图 / LangGraph（最小脚手架，最大运行环境）。
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable

from coreagent.conversation import Conversation
from coreagent.injection import build_reminder_message
from coreagent.providers.base import ChunkType, StreamChunk
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
# 一个用户输入内模型调用次数上限（轮数上限）。
MAX_ROUNDS = 25
# 某轮模型只产出工具调用 / 空文本时的占位答复，避免写入空 assistant 消息。
_EMPTY_REPLY_PLACEHOLDER = "（已完成工具调用）"

# 循环退出原因（LOOP_DONE.exit_reason）。
EXIT_NATURAL = "natural"      # 模型本轮无工具调用，自然完成
EXIT_CAP = "cap"             # 达到轮数上限
EXIT_CANCELLED = "cancelled"  # 收到取消信号

ConfirmCb = Callable[[dict], Awaitable[bool]]
OnToolCb = Callable[[str, dict, ToolResult | None], None]
OnRetryCb = Callable[[int, int], None]


def _result_block(tc: dict, content: str, is_error: bool) -> dict:
    """组装一条 tool_result（供 conversation.add_tool_results 回灌）。"""
    return {"tool_use_id": tc["id"], "content": content, "is_error": is_error}


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


async def _run_tools(
    tool_calls: list[dict],
    registry: ToolRegistry,
    *,
    confirm: ConfirmCb | None,
    on_tool: OnToolCb | None,
    cancel,
    plan_only: bool,
    plan_items: list,
) -> tuple[list[dict], bool]:
    """执行一轮工具调用：读类并发、写类串行；plan 拦截写类；取消时补齐占位。

    返回 (results, cancelled)：
      - results 按模型**原调用顺序**排列（确定性、可重放）；
      - on_tool("result", ...) 按**完成先后** emit（实时进度，不要求与调用序一致）——
        两者顺序语义有意分开：历史要稳定、UI 要实时；
      - cancelled 表示中途被取消（仍无真实结果的 tool_use 已补「已取消」占位）。
    """

    def cancelled_now() -> bool:
        return cancel is not None and cancel.is_set()

    results: list[dict | None] = [None] * len(tool_calls)

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

    # ── 读类并发：tool_result 按原索引回灌（调用序）、on_tool 按完成序 emit ──
    if reads and not cancelled_now():
        for _, tc in reads:
            if on_tool is not None:
                on_tool("call", tc, None)

        async def _run_read(idx: int, tc: dict) -> tuple[int, ToolResult]:
            result = await registry.execute(tc["name"], tc["input"])
            if on_tool is not None:
                on_tool("result", tc, result)  # 完成序 emit（哪个先跑完哪个先反馈）
            return idx, result

        for idx, result in await asyncio.gather(*(_run_read(i, t) for i, t in reads)):
            results[idx] = _result_block(tool_calls[idx], result.content, not result.success)

    # ── 写类串行：plan 拦截 / 确认 / 执行；取消后余下补占位 ──
    for idx, tc in writes:
        if cancelled_now():
            break
        if on_tool is not None:
            on_tool("call", tc, None)

        if plan_only:
            # plan-only：不执行、不 confirm，记一条计划项，回灌「已记录未执行」结构化结果。
            plan_items.append(_format_plan_item(tc))
            recorded = ToolResult.ok(PLAN_RECORDED_MESSAGE)
            if on_tool is not None:
                on_tool("result", tc, recorded)
            results[idx] = _result_block(tc, PLAN_RECORDED_MESSAGE, False)
            continue

        approved = await confirm(tc) if confirm is not None else False
        if cancelled_now():
            # 确认期间被取消：该写类「已发起但被中断未完成」→ 留待补占位。
            break
        if not approved:
            rejected = ToolResult.fail(REJECTED_MESSAGE)
            if on_tool is not None:
                on_tool("rejected", tc, rejected)
            results[idx] = _result_block(tc, REJECTED_MESSAGE, True)
            continue

        result = await registry.execute(tc["name"], tc["input"])
        if on_tool is not None:
            on_tool("result", tc, result)
        results[idx] = _result_block(tc, result.content, not result.success)

    # ── 取消补齐：仍无真实结果的 tool_use 一律补「已取消」占位（配平、可重放）；
    #    已完成的（含正在执行、等其跑完的廉价读类）保留真实结果，不替换。 ──
    cancelled = cancelled_now()
    if cancelled:
        for idx, tc in enumerate(tool_calls):
            if results[idx] is None:
                results[idx] = _result_block(tc, CANCELLED_MESSAGE, True)

    # 非取消路径下每个 tool_use 必有真实结果（读全 await、写全处理）；不留 None。
    return [r if r is not None else _result_block(tool_calls[i], CANCELLED_MESSAGE, True)
            for i, r in enumerate(results)], cancelled


async def run_agent_turn(
    provider,
    conversation: Conversation,
    registry: ToolRegistry,
    system: str | None = None,
    *,
    env_block: str | None = None,
    confirm: ConfirmCb | None = None,
    on_tool: OnToolCb | None = None,
    on_retry: OnRetryCb | None = None,
    cancel=None,
    plan_only: bool = False,
) -> AsyncGenerator[StreamChunk, None]:
    """多轮 Agent Loop：调模型 → 分类执行工具 → 回灌 → 检查终止条件，直到自然完成 / 上限 / 取消 / 错误。

    - ``cancel``：可选取消令牌（任何带 ``is_set()`` 的对象，如 ``asyncio.Event``），每轮开始与每个
      工具执行前检查；取消后优雅补齐 tool_result、emit 循环结束（原因 = 取消）。
    - ``plan_only``：plan-only 开关。读类照常执行，写类被拦截记为计划项；计划列表随循环结束信号交还。
    - ``env_block``：会话启动时快照的环境块（带 <env> 标签）；作为 system 尾部块（不进可缓存前缀）注入，
      **不写入** conversation。缺省 None 时不注入（与既有调用行为一致）。
    """
    tools_api = registry.to_api_tools()
    # 稳定模块块（system）+ 环境快照块（env_block）：装配为结构化 system 交 provider 分通道缓存；
    # env 整会话不变、不写历史。无 env_block 时退回纯字符串（向后兼容）。
    request_system: str | list | None = [system, env_block] if env_block else system
    plan_items: list = []
    committed = False  # 本回合是否已写入 assistant / tool_result（决定取消 / 错误时是否回滚 user 提问）
    round_index = 0

    def loop_done(reason: str) -> StreamChunk:
        return StreamChunk(
            ChunkType.LOOP_DONE,
            exit_reason=reason,
            plan=list(plan_items) if plan_only else None,
        )

    while True:
        # 终止条件①：取消（每轮开始检查）。
        if cancel is not None and cancel.is_set():
            if not committed:
                _rollback_uncommitted_user(conversation)
            yield loop_done(EXIT_CANCELLED)
            return

        # 终止条件②：达轮数上限——不再调模型，emit 循环结束（原因 = 上限）。
        if round_index >= MAX_ROUNDS:
            yield loop_done(EXIT_CAP)
            return

        round_index += 1
        yield StreamChunk(ChunkType.TURN_START, round_index=round_index)

        # ── 调模型（带工具流式）；不可恢复错误（退避耗尽后上抛）→ 循环错误信号并终止 ──
        text_parts: list[str] = []
        blocks: list | None = None
        tool_calls: list | None = None
        stop_reason: str | None = None
        # 动态注入：按（plan_only、轮序）产出临时提醒，追加到滚动断点之后的 messages 尾部；
        # 每请求重建、**不写入** conversation（历史仍 append-only 可重放）。
        request_messages = conversation.get_messages()
        reminder = build_reminder_message(plan_only=plan_only, round_index=round_index)
        if reminder is not None:
            request_messages = request_messages + [reminder]

        try:
            async for chunk in stream_with_retry(
                provider,
                request_messages,
                system=request_system,
                tools=tools_api,
                on_retry=on_retry,
            ):
                if chunk.type == ChunkType.TEXT:
                    text_parts.append(chunk.content)
                elif chunk.type == ChunkType.DONE:
                    blocks = chunk.blocks
                    tool_calls = chunk.tool_calls
                    stop_reason = chunk.stop_reason
                yield chunk
        except Exception as exc:  # noqa: BLE001 —— 退避耗尽后的不可恢复错误（区别于单个工具失败）
            logger.warning("模型调用不可恢复失败：%r", exc)
            if not committed:
                _rollback_uncommitted_user(conversation)
            yield StreamChunk(ChunkType.LOOP_ERROR, error_type=type(exc).__name__)
            return

        text = "".join(text_parts)
        yield StreamChunk(ChunkType.TURN_END, round_index=round_index, stop_reason=stop_reason)

        # 终止条件③：本轮无工具调用 → 自然完成（与 #0003 纯对话 / 单轮路径产出一致）。
        if not tool_calls:
            if blocks is not None:
                conversation.add_assistant(text, blocks=blocks)
            else:
                conversation.add_assistant(text or _EMPTY_REPLY_PLACEHOLDER)
            committed = True
            yield loop_done(EXIT_NATURAL)
            return

        # 有工具调用：记录含 tool_use 的 assistant（供回灌多轮）。
        if blocks is not None:
            conversation.add_assistant_blocks(blocks)
        else:
            conversation.add_assistant(text)
        committed = True

        # 分类执行（读并发 / 写串行 / plan 拦截 / 取消补齐）→ 回灌 tool_result（原调用序）。
        results, cancelled = await _run_tools(
            tool_calls,
            registry,
            confirm=confirm,
            on_tool=on_tool,
            cancel=cancel,
            plan_only=plan_only,
            plan_items=plan_items,
        )
        conversation.add_tool_results(results)

        # 终止条件④：执行中途被取消 → 优雅收尾（历史已配平 tool_use / tool_result）。
        if cancelled:
            yield loop_done(EXIT_CANCELLED)
            return

        # 否则进入下一轮（回灌后再调模型）。
