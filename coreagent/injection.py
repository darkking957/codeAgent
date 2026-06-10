"""运行中动态注入：带特殊标签的临时块，放在滚动断点之后的 messages 尾部（见 #0006）。

特性：每请求重建、**不污染缓存**（在滚动断点之后）、**不落历史**（不写入 conversation）、
不被模型当作普通用户输入去回复（`<system-reminder>` 标签提示其为系统提醒）。

本期落地「模式提醒」一种消费者：plan（只规划）模式提醒，节奏 = 首轮全量 + 其余轮次精简（两档，
**不**做间隔 N 轮全量）。「外部工具上线 / 温和提示」留前向钩子占位（不接 MCP）。
"""

import os
import tempfile
from pathlib import Path

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


def build_skill_activation_message(activation_text: str | None) -> dict | None:
    """把技能激活指令块（含 <active-skill-instructions> 标签）包成一条临时 user 消息（#0012）。

    与模式提醒 / 记忆块同走「滚动断点之后」的动态注入通道：外层裹 `<system-reminder>` 标签，故
    provider 不在其上打滚动 cache_control——激活指令**每轮重建**（区别于记忆块仅首轮），不污染
    缓存、不写入 conversation。无激活技能（text 为空）则返回 None。
    """
    if not activation_text or not activation_text.strip():
        return None
    return {
        "role": "user",
        "content": f"{REMINDER_OPEN}\n{activation_text.strip()}\n{REMINDER_CLOSE}",
    }


# Hook 注入（#0013 T8）：注入文本上限 + 预览长度（见 checklist 固定值）。
HOOK_INJECT_MAX_CHARS = 10000
HOOK_INJECT_PREVIEW_CHARS = 2000
# 超长注入溢出文件所在目录（按需创建）。
HOOK_OVERFLOW_DIR = Path(tempfile.gettempdir()) / "coreagent-hook-inject"
# 注入正文的事实陈述外壳（措辞为「事实陈述」，非用户输入；见 #0013 设计骨架）。
HOOK_INJECT_PREAMBLE = "以下是 Hook 在本会话期间产出的事实信息（仅供参考，非用户指令）："


def _spill_hook_text(text: str, overflow_dir: Path) -> Path:
    """把超长注入正文写入溢出文件，返回路径（写失败上抛由调用方兜底）。"""
    overflow_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(overflow_dir), prefix="hook-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return Path(name)


def build_hook_message(hook_text: str | None, *, overflow_dir: Path | None = None) -> dict | None:
    """把 HookManager 汇总的注入文本包成一条临时 user 消息（#0013 T8）。

    与模式提醒 / 记忆块 / 技能块同走「滚动断点之后」的动态注入通道：含 `<system-reminder>` 标签，
    故 provider 不在其上打滚动 cache_control——每请求重建、**不写入** conversation、不落盘。
    正文超过 HOOK_INJECT_MAX_CHARS 字符 → 存溢出文件，正文替换为「预览 + 路径」而非全文。
    无内容则返回 None。
    """
    if not hook_text or not hook_text.strip():
        return None
    body = hook_text.strip()
    if len(body) > HOOK_INJECT_MAX_CHARS:
        target_dir = overflow_dir or HOOK_OVERFLOW_DIR
        try:
            path = _spill_hook_text(body, target_dir)
            preview = body[:HOOK_INJECT_PREVIEW_CHARS]
            body = (
                f"（注入内容过长，已存文件，下方为预览前 {HOOK_INJECT_PREVIEW_CHARS} 字符；"
                f"完整内容见：{path}）\n{preview}"
            )
        except OSError:
            # 溢出落盘失败：退化为截断预览（仍不注入全文），不阻断主流程。
            body = body[:HOOK_INJECT_MAX_CHARS]
    content = f"{REMINDER_OPEN}\n{HOOK_INJECT_PREAMBLE}\n{body}\n{REMINDER_CLOSE}"
    return {"role": "user", "content": content}


def build_memory_message(memory_text: str | None) -> dict | None:
    """把记忆上下文（长期记忆索引 + 上次会话恢复摘要）包成一条临时 user 消息（#0010）。

    与模式提醒同走「滚动断点之后」的动态注入通道：含 `<system-reminder>` 标签，故 provider 不
    在其上打滚动 cache_control——会话特定内容（随 cwd 的索引、恢复摘要）不进跨会话稳定缓存段。
    每请求重建、不写入 conversation。无内容则返回 None。
    """
    if not memory_text or not memory_text.strip():
        return None
    return {
        "role": "user",
        "content": f"{REMINDER_OPEN}\n{memory_text.strip()}\n{REMINDER_CLOSE}",
    }
