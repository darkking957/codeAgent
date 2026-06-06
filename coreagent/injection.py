"""运行中动态注入：带特殊标签的临时块，放在滚动断点之后的 messages 尾部（见 #0006）。

特性：每请求重建、**不污染缓存**（在滚动断点之后）、**不落历史**（不写入 conversation）、
不被模型当作普通用户输入去回复（`<system-reminder>` 标签提示其为系统提醒）。

本期落地「模式提醒」一种消费者：plan（只规划）模式提醒，节奏 = 首轮全量 + 其余轮次精简（两档，
**不**做间隔 N 轮全量）。「外部工具上线 / 温和提示」留前向钩子占位（不接 MCP）。
"""

# 动态提醒标签（见 checklist 固定值）。
REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"

# plan 全量提醒（首轮）：须含「plan 模式」「会被记录」「不会执行」（见 checklist 固定值）。
PLAN_REMINDER_FULL = (
    "你当前处于 plan 模式（只规划、不执行）：请继续提议需要的写操作与命令，"
    "它们会被记录为计划项，但不会执行。据此把方案规划清楚。"
)
# plan 精简提醒（第 2 轮起）：含「plan 模式」、不含整句「会被记录」、明显短于全量。
PLAN_REMINDER_BRIEF = "提醒：仍处于 plan 模式，继续只规划、不执行写操作。"


def _plan_reminder_body(plan_only: bool, round_index: int) -> str:
    """按（plan_only、轮序）选 plan 提醒文案；非 plan 模式返回空串。"""
    if not plan_only:
        return ""
    return PLAN_REMINDER_FULL if round_index <= 1 else PLAN_REMINDER_BRIEF


def _external_tools_hint() -> str:
    """前向钩子占位：外部工具（MCP）上线后在此产出「新工具可用」温和提示。

    本期不接 MCP，恒为空；保留入口供后续接入（见 #0006 Out of Scope）。
    """
    return ""


def build_reminder(*, plan_only: bool, round_index: int) -> str | None:
    """供给入口：按当前状态产出带标签的临时提醒文本；无可注入则返回 None。"""
    parts: list[str] = []
    plan_body = _plan_reminder_body(plan_only, round_index)
    if plan_body:
        parts.append(plan_body)
    hint = _external_tools_hint()
    if hint:
        parts.append(hint)
    if not parts:
        return None
    return f"{REMINDER_OPEN}\n" + "\n".join(parts) + f"\n{REMINDER_CLOSE}"


def build_reminder_message(*, plan_only: bool, round_index: int) -> dict | None:
    """把提醒文本包成一条临时 user 消息（供追加到滚动断点之后）；无则 None。

    每请求重建、不写入 conversation；provider 据内容含 `<system-reminder>` 识别其为临时块，
    故不在其上打滚动 cache_control（见 anthropic provider 的 messages 装配）。
    """
    text = build_reminder(plan_only=plan_only, round_index=round_index)
    if text is None:
        return None
    return {"role": "user", "content": text}
