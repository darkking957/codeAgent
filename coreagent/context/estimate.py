"""T3｜token 近似估算（纯函数，无 I/O）。

策略：锚定上一次 API 请求的服务端输入计数，仅对其后**新增消息**按字符近似估算增量；
无锚点（首个请求 / 已加载的大历史）时退回全量字符近似。除数见 constants.CHARS_PER_TOKEN。

字符统计覆盖：user 字符串、tool_result 块文本、assistant 文本 / thinking / 工具入参（name+input）。
"""

import json

from coreagent.context.constants import CHARS_PER_TOKEN


def _block_chars(block) -> int:
    """统计单个内容块的字符数（递归处理 tool_result 的块列表内容）。"""
    if isinstance(block, str):
        return len(block)
    if not isinstance(block, dict):
        return len(str(block))

    total = 0
    btype = block.get("type")
    # 文本 / thinking 块。
    text = block.get("text")
    if isinstance(text, str):
        total += len(text)
    thinking = block.get("thinking")
    if isinstance(thinking, str):
        total += len(thinking)
    # 工具调用块：名称 + 入参 JSON。
    if btype == "tool_use":
        total += len(block.get("name", ""))
        try:
            total += len(json.dumps(block.get("input", {}), ensure_ascii=False))
        except (TypeError, ValueError):
            total += len(str(block.get("input", {})))
    # 工具结果块：content 可能是字符串或块列表。
    if btype == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for sub in content:
                total += _block_chars(sub)
    return total


def message_chars(msg: dict) -> int:
    """统计单条消息的字符数（字符串内容直接计长；块列表逐块累加）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_block_chars(b) for b in content)
    return 0


def message_tokens(msg: dict) -> int:
    """单条消息的近似 token 数（字符数 / 除数）。"""
    return message_chars(msg) // CHARS_PER_TOKEN


def estimate_input_tokens(
    messages: list[dict],
    anchor_input: int | None = None,
    anchor_index: int | None = None,
) -> int:
    """估算当前请求的输入 token 数。

    - 有锚点（anchor_input 且 anchor_index 合法）：估算 = 锚点输入 + 锚点序号之后消息字符数 / 除数。
      锚点输入已含 system/tools 与锚点处全部消息的真实计数，只对其后新增消息近似累加。
    - 无锚点 / 锚点失效（如压缩后序号越界）：退回全量字符近似。
    """
    if (
        anchor_input is not None
        and anchor_index is not None
        and 0 <= anchor_index <= len(messages)
    ):
        new_chars = sum(message_chars(m) for m in messages[anchor_index:])
        return anchor_input + new_chars // CHARS_PER_TOKEN
    total_chars = sum(message_chars(m) for m in messages)
    return total_chars // CHARS_PER_TOKEN
