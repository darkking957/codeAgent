"""事件 → SSE 帧序列化（#0021，哑转发器）。

把每个 #0018 类型化事件**原样**映射成一个 ``(event 名, data 字典)``，再格式化为一个 SSE 帧
（``event:`` 行 + ``data:`` 行 + 空行）。覆盖全部七类事件；工具结果事件展开其 ``ToolResult`` 的
content / success 与「被拒 / 失败」标记。

哑转发器铁律：一事件一帧，**不重塑含义、不合并、不新增字段、不算 diff**——diff 一律由前端从工具
调用 input 重建。本模块是**纯函数**：无 I/O、不触盘、不发网、不复制引擎逻辑；仅依赖标准库 ``json``
与 #0018 事件定义。HTTP 库只活在 ``app`` 层，本模块不 import 它（守住单向依赖）。

``data`` 用 ``json.dumps`` 序列化为**一行**（库会把字符串内的换行转义为 ``\\n``，故含多行内容的
工具结果 / 调用 input 仍是单物理行，不破坏 SSE「一帧一 data 行」帧格式）。

**web 层帧（#0023）**：审批请求 / 审批已决 / 文件 diff **不是** #0018 引擎事件，而是 web 层在同一
SSE 流上复用的帧（``WebFrame``）。它们由确认回调 / diff 钩子产出、与引擎事件**同队列**承载、消费侧
按类型分派序列化（见 ``app.py``）。铁律：**不新增 #0018 引擎事件**（``events.py`` 零改动），审批 / diff
语义纯活在 web 层（``sse.py`` + ``app.py``），引擎不感知。
"""

import json
from dataclasses import dataclass

from coreagent.events import (
    Retry,
    RunDone,
    RunError,
    TextDelta,
    ToolCall,
    ToolResultEvent,
    TurnStart,
)

# web 层帧的事件名（前端 dispatchLive 据此分支；与 #0018 引擎事件名不重叠）。
APPROVAL_REQUEST = "approval_request"
APPROVAL_DECIDED = "approval_decided"
FILE_DIFF = "file_diff"


def event_to_frame_parts(event) -> tuple[str, dict]:
    """把一个 #0018 事件映射成 ``(event 名, data 字典)``；未知类型抛 ``TypeError``。

    字段名与 checklist「SSE 帧契约」表逐字对齐：枚举一律取 ``.value``（``kind`` / ``reason``），
    工具结果事件展开 ``content`` / ``success`` / ``rejected`` / ``is_error`` 四个标记字段。
    """
    if isinstance(event, TextDelta):
        return "text_delta", {"kind": event.kind.value, "text": event.text}
    if isinstance(event, ToolCall):
        return "tool_call", {"call": event.call}
    if isinstance(event, ToolResultEvent):
        return "tool_result", {
            "call": event.call,
            "content": event.result.content,
            "success": event.result.success,
            "rejected": event.rejected,
            "is_error": event.is_error,
        }
    if isinstance(event, TurnStart):
        return "turn_start", {"round_index": event.round_index}
    if isinstance(event, Retry):
        return "retry", {"attempt": event.attempt, "wait": event.wait}
    if isinstance(event, RunDone):
        return "run_done", {
            "reason": event.reason.value,
            "plan": event.plan,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
        }
    if isinstance(event, RunError):
        return "run_error", {"error_type": event.error_type}
    raise TypeError(f"未知事件类型，无法序列化为 SSE 帧：{type(event).__name__}")


def event_to_sse(event) -> str:
    """把一个 #0018 事件格式化为一个 SSE 帧字符串：``event: <名>`` 行 + ``data: <一行 JSON>`` 行 + 空行。"""
    name, data = event_to_frame_parts(event)
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {name}\ndata: {payload}\n\n"


# ════════════════════════════════════════════════════════════════════════════════
# web 层帧（#0023）：审批请求 / 审批已决 / 文件 diff —— 非引擎事件，同流复用
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class WebFrame:
    """web 层 SSE 帧（非 #0018 引擎事件）：承载 ``event`` 名 + ``data`` 字典。

    与引擎事件**同队列**承载、消费侧按 ``isinstance`` 分派（引擎事件走 ``event_to_sse``、本类走
    ``web_frame_to_sse``）。审批 / diff 语义纯活在 web 层，引擎不感知（``events.py`` 零改动）。
    """

    event: str
    data: dict


def web_frame_to_sse(frame: WebFrame) -> str:
    """把一个 web 层帧格式化为 SSE 帧（与 ``event_to_sse`` 同格式，区别仅在来源类型）。"""
    payload = json.dumps(frame.data, ensure_ascii=False)
    return f"event: {frame.event}\ndata: {payload}\n\n"


def approval_request_frame(tool_id: str, name: str, tool_input: dict) -> WebFrame:
    """审批请求帧：携带工具调用标识 + 工具名 + **完整入参**（命令 / 参数对用户完全可读）。"""
    return WebFrame(APPROVAL_REQUEST, {"tool_id": tool_id, "name": name, "input": tool_input})


def approval_decided_frame(tool_id: str, decision: str) -> WebFrame:
    """审批已决帧：携带工具调用标识 + 落定的四档决策值（前端据此关弹窗）。"""
    return WebFrame(APPROVAL_DECIDED, {"tool_id": tool_id, "decision": decision})


def file_diff_frame(tool_id: str, path: str, diff: str) -> WebFrame:
    """文件 diff 帧：写/改类工具执行**前后真实快照**的统一 diff 文本（不由入参推导）。"""
    return WebFrame(FILE_DIFF, {"tool_id": tool_id, "path": path, "diff": diff})
