"""T4｜第一层预防：大工具结果卸载（单条消息粒度，每请求前跑）。

对一条「含 tool_result 的 user 消息」：
  ① 单个结果超阈值 → 内容写盘到稳定目录、原位替换为「预览 + 路径 + 重读提示」；
  ② 该消息工具结果合计仍超阈值 → 按体量从大到小依次卸载直至达标。
只动 tool_result 块，绝不碰用户原始消息 / assistant 块。卸载文件落稳定目录持久化（供 read_file 重读）。
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from coreagent.context.constants import (
    MESSAGE_TOTAL_THRESHOLD,
    OFFLOAD_NOTICE,
    PREVIEW_MAX_CHARS,
    PREVIEW_MAX_LINES,
    SINGLE_RESULT_THRESHOLD,
)

logger = logging.getLogger(__name__)

# 稳定卸载目录（非随机临时、进程退出不消失，供 read_file 按路径重读）。
DEFAULT_OFFLOAD_DIR = Path("~/.config/coreagent/offload").expanduser()


def _is_tool_result(block) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_result"


def _result_text(block: dict) -> str:
    """取 tool_result 块的文本内容（content 多为字符串，亦兼容块列表）。"""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content) if content is not None else ""


def build_preview(text: str) -> str:
    """卸载预览：前 PREVIEW_MAX_LINES 行，且不超过 PREVIEW_MAX_CHARS 字符（先到为准）。"""
    preview = "\n".join(text.splitlines()[:PREVIEW_MAX_LINES])
    if len(preview) > PREVIEW_MAX_CHARS:
        preview = preview[:PREVIEW_MAX_CHARS]
    return preview


def _offloaded_content(text: str, path: Path) -> str:
    """卸载后替换进 tool_result 的短文本：通知 + 完整路径 + 重读提示 + 预览。"""
    return (
        f"{OFFLOAD_NOTICE}\n"
        f"完整内容路径：{path}\n"
        f"如需完整内容，请用 read_file 读取上述路径，不要照预览臆测。\n"
        f"--- 预览（前 {PREVIEW_MAX_LINES} 行 / {PREVIEW_MAX_CHARS} 字符以内）---\n"
        f"{build_preview(text)}"
    )


def _write_offload(text: str, offload_dir: Path) -> Path:
    """把全文原子写入稳定目录；文件名按内容哈希，天然去重、可重读。"""
    offload_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    dest = offload_dir / f"offload_{digest}.txt"
    if dest.exists():
        return dest
    fd, tmp = tempfile.mkstemp(dir=str(offload_dir), prefix="offload_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def _message_result_chars(content: list) -> int:
    return sum(len(_result_text(b)) for b in content if _is_tool_result(b))


def offload_message(msg: dict, offload_dir: Path = DEFAULT_OFFLOAD_DIR) -> list[Path]:
    """对单条 user(tool_result) 消息原地卸载超阈值的结果；返回本次卸载文件路径列表。

    非 user / 非块列表内容（如用户原始字符串消息、assistant 块）一律不动，返回空列表。
    """
    if msg.get("role") != "user":
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []

    offloaded_paths: list[Path] = []
    offloaded_ids: set[int] = set()

    # ① 单个结果超阈值即卸载。
    for block in content:
        if _is_tool_result(block):
            text = _result_text(block)
            if len(text) > SINGLE_RESULT_THRESHOLD:
                path = _write_offload(text, offload_dir)
                block["content"] = _offloaded_content(text, path)
                offloaded_paths.append(path)
                offloaded_ids.add(id(block))

    # ② 合计仍超阈值 → 在未卸载者中挑最大依次卸载，直至达标或无可卸载。
    while _message_result_chars(content) > MESSAGE_TOTAL_THRESHOLD:
        candidates = [
            b for b in content if _is_tool_result(b) and id(b) not in offloaded_ids
        ]
        if not candidates:
            break
        biggest = max(candidates, key=lambda b: len(_result_text(b)))
        text = _result_text(biggest)
        path = _write_offload(text, offload_dir)
        biggest["content"] = _offloaded_content(text, path)
        offloaded_paths.append(path)
        offloaded_ids.add(id(biggest))

    return offloaded_paths


def offload_conversation(
    messages: list[dict], offload_dir: Path = DEFAULT_OFFLOAD_DIR
) -> list[Path]:
    """遍历会话所有消息，对含大 tool_result 的 user 消息原地卸载；返回全部卸载文件路径。"""
    all_paths: list[Path] = []
    for msg in messages:
        try:
            all_paths.extend(offload_message(msg, offload_dir))
        except OSError as e:  # 单条卸载落盘失败：记告警、跳过，不拖垮整体（降级为不卸载该条）。
            logger.warning("工具结果卸载失败，跳过该条：%r", e)
    return all_paths
