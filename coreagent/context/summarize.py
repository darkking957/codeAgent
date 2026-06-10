"""T5/T6｜第二层兜底：摘要 prompt 构造 + 调 LLM + 解析 + 熔断；keep-recent 切分 + 边界消息。

纪律（T5）：摘要 prompt 禁用工具、先草稿后正式（草稿用完即弃）、固定 5 段、用户原文保留。
配对（T6）：从尾按 token 往回数凑保留区；切分点须落在干净边界——保留区不得以无配对的
tool_result 开头（必要时把切分点上移到最近的「用户原始字符串消息」）。
"""

import logging

from coreagent.context.constants import (
    BOUNDARY_MESSAGE,
    FORMAL_SUMMARY_MARKER,
    KEEP_RECENT_MIN_MSGS,
    KEEP_RECENT_TOKENS,
    SUMMARY_FAILURE_LIMIT,
    SUMMARY_INSTRUCTION,
    SUMMARY_MESSAGE_HEADER,
    SUMMARY_SYSTEM,
    USER_ORIGINALS_HEADER,
)
from coreagent.context.estimate import message_tokens
from coreagent.providers.base import ChunkType

logger = logging.getLogger(__name__)


# ── 摘要请求构造 + 解析（T5）─────────────────────────────────────────────────────────

def _render_block(block) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return str(block)
    btype = block.get("type")
    if btype == "text":
        return block.get("text", "")
    if btype == "thinking":
        return f"(思考) {block.get('thinking', '')}"
    if btype == "tool_use":
        return f"(调用工具 {block.get('name', '?')}) {block.get('input', {})}"
    if btype == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        return f"(工具结果) {content}"
    return str(block)


def render_history_for_summary(messages: list[dict]) -> str:
    """把待压缩历史拍平为纯文本（供摘要模型阅读）。"""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
        elif isinstance(content, list):
            for b in content:
                lines.append(f"[{role}] {_render_block(b)}")
    return "\n".join(lines)


def build_summary_request(summary_region: list[dict]) -> tuple[str, list[dict]]:
    """构造摘要请求：(system, messages)。messages 为单条 user，含纪律 + 待压缩历史。"""
    history_text = render_history_for_summary(summary_region)
    user_msg = {"role": "user", "content": SUMMARY_INSTRUCTION + history_text}
    return SUMMARY_SYSTEM, [user_msg]


def parse_summary(raw: str) -> str:
    """解析模型输出：丢弃草稿、只留正式摘要（取分隔标记之后的最后一段）；无标记则整体返回。"""
    if FORMAL_SUMMARY_MARKER in raw:
        return raw.split(FORMAL_SUMMARY_MARKER)[-1].strip()
    return raw.strip()


def _user_originals(summary_region: list[dict]) -> list[str]:
    """摘要区里的用户原始消息（字符串内容的 user 消息）原文。"""
    return [
        m["content"]
        for m in summary_region
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]


def build_summary_message(summary_text: str, summary_region: list[dict]) -> dict:
    """组装摘要消息（user 角色）：正式摘要 + 用户原始消息原文区（保证原文出现、不被改写）。"""
    body = f"{SUMMARY_MESSAGE_HEADER}\n{summary_text}"
    originals = _user_originals(summary_region)
    if originals:
        body += f"\n\n{USER_ORIGINALS_HEADER}\n" + "\n".join(originals)
    return {"role": "user", "content": body}


def build_boundary_message() -> dict:
    """压缩边界消息（assistant 角色）：与摘要(user)、保留区首条(user 原始消息)交替，保证 API 配平。"""
    return {"role": "assistant", "content": BOUNDARY_MESSAGE}


class Summarizer:
    """调 provider 生成摘要 + 连续失败熔断。

    熔断：连续失败计数达 SUMMARY_FAILURE_LIMIT 即熔断（tripped），之后 summarize 直接降级返回
    None、**不再发起 provider 调用**；任一次成功则计数清零。
    """

    def __init__(self, provider, failure_limit: int = SUMMARY_FAILURE_LIMIT) -> None:
        self.provider = provider
        self.failure_limit = failure_limit
        self.consecutive_failures = 0
        self.tripped = False

    async def summarize(self, summary_region: list[dict]) -> str | None:
        """生成正式摘要文本；熔断 / 调用失败 / 空区间一律返回 None（上层据此降级不压缩）。"""
        if self.tripped:
            logger.warning("摘要已熔断，跳过本次摘要尝试（降级为不压缩）")
            return None
        if not summary_region:
            return None
        system, messages = build_summary_request(summary_region)
        try:
            text_parts: list[str] = []
            # 关键：摘要请求**不传 tools**（纪律：禁止调用任何工具）。
            async for chunk in self.provider.stream_chat(messages, system=system, tools=None):
                if chunk.type == ChunkType.TEXT:
                    text_parts.append(chunk.content)
            self.consecutive_failures = 0  # 成功 → 清零
            return parse_summary("".join(text_parts))
        except Exception as e:  # noqa: BLE001 —— 摘要失败不崩溃，计数 + 到上限熔断
            self.consecutive_failures += 1
            logger.warning(
                "摘要调用失败（连续第 %d 次）：%r", self.consecutive_failures, e
            )
            if self.consecutive_failures >= self.failure_limit:
                self.tripped = True
                logger.warning(
                    "摘要连续失败达上限（%d 次），已熔断：停止后续摘要尝试，避免死循环",
                    self.failure_limit,
                )
            return None


# ── keep-recent 切分（T6）────────────────────────────────────────────────────────────

def _is_clean_start(msg: dict) -> bool:
    """干净边界：保留区首条须是「用户原始字符串消息」（既非孤立 tool_result，亦非 assistant）。

    如此 [摘要(user)] + [边界(assistant)] + [保留区首条(user)] 三者角色交替，满足 API 配对/可重放。
    """
    return msg.get("role") == "user" and isinstance(msg.get("content"), str)


def split_keep_recent(
    messages: list[dict],
    keep_tokens: int = KEEP_RECENT_TOKENS,
    keep_min_msgs: int = KEEP_RECENT_MIN_MSGS,
) -> int:
    """计算切分点 cutoff：messages[:cutoff] 为摘要区、messages[cutoff:] 为保留区。

    保留区 = 从尾累计约 keep_tokens token、且至少 keep_min_msgs 条（取更多者 = 更小 cutoff），
    再把 cutoff **上移**到最近的干净边界。cutoff==0 表示无可摘要区间（调用方应跳过压缩）。
    """
    n = len(messages)
    if n == 0:
        return 0

    # 从尾累计 token 达 keep_tokens 的起点。
    acc = 0
    idx_tokens = 0
    for i in range(n - 1, -1, -1):
        acc += message_tokens(messages[i])
        idx_tokens = i
        if acc >= keep_tokens:
            break
    # 至少 keep_min_msgs 条的起点。
    idx_count = max(0, n - keep_min_msgs)
    # 取保留更多消息者（更小 index）。
    cutoff = min(idx_tokens, idx_count)

    # 上移到干净边界：保留区不得以无配对 tool_result（或 assistant）开头。
    while cutoff > 0 and not _is_clean_start(messages[cutoff]):
        cutoff -= 1
    return cutoff
