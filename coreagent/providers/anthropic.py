import json
import logging
from collections.abc import AsyncIterator

import anthropic as _anthropic

from coreagent.config import Config
from coreagent.environment import ENV_TAG_OPEN
from coreagent.injection import REMINDER_OPEN
from coreagent.providers.base import BaseProvider, ChunkType, StreamChunk

logger = logging.getLogger(__name__)

_CACHE_CONTROL = {"type": "ephemeral"}


# ── 缓存通道装配（#0006 T4/T5）：把稳定内容放可缓存前缀、变化内容放断点之后 ──────────────

def _assemble_system(system: str | list | None) -> list | None:
    """把 `system` 组成块列表：稳定模块块打 cache_control、环境快照块（含 <env>）不打。

    向后兼容：纯字符串 → 单个稳定块（打 cache_control）。
    """
    if not system:
        return None
    items = [system] if isinstance(system, str) else system
    blocks: list[dict] = []
    for item in items:
        text = item if isinstance(item, str) else item.get("text", "")
        block: dict = {"type": "text", "text": text}
        # 环境块是 session-specific，绝不进可缓存前缀（否则破坏跨会话命中）。
        if ENV_TAG_OPEN not in text:
            block["cache_control"] = _CACHE_CONTROL
        blocks.append(block)
    return blocks


def _assemble_tools(tools: list[dict] | None) -> list | None:
    """工具清单属可缓存区：在末尾一个工具上打 cache_control（一个断点覆盖整段 tools）。"""
    if not tools:
        return None
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": _CACHE_CONTROL}
    return out


def _is_reminder(msg: dict) -> bool:
    """临时提醒消息：字符串内容且含 `<system-reminder>` 标签（每请求重建、不打缓存断点）。"""
    content = msg.get("content")
    return isinstance(content, str) and REMINDER_OPEN in content


def _with_rolling_cache(msg: dict) -> dict:
    """在一条消息的末块上打滚动 cache_control（字符串内容转为带标签的单文本块）。"""
    content = msg.get("content")
    if isinstance(content, str):
        new_content = [{"type": "text", "text": content, "cache_control": _CACHE_CONTROL}]
    elif isinstance(content, list) and content:
        new_content = [dict(b) for b in content]
        new_content[-1] = {**new_content[-1], "cache_control": _CACHE_CONTROL}
    else:
        return msg
    return {**msg, "content": new_content}


def _assemble_messages(messages: list[dict]) -> list[dict]:
    """在「最后一条已定型消息」打滚动 cache_control；临时提醒（在其后）不打。"""
    out = [dict(m) for m in messages]
    for i in range(len(out) - 1, -1, -1):
        if not _is_reminder(out[i]):
            out[i] = _with_rolling_cache(out[i])
            break
    return out


class AnthropicProvider(BaseProvider):
    def __init__(self, config: Config) -> None:
        self.config = config
        kwargs: dict = {"api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = _anthropic.AsyncAnthropic(**kwargs)

    async def stream_chat(
        self,
        messages: list[dict],
        system: str | list | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        max_tokens = (
            self.config.thinking_max_tokens
            if self.config.thinking.enabled
            else self.config.max_tokens
        )
        params: dict = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            # 滚动断点：稳定前缀（tools + system）+ 历史末条已定型消息可跨轮命中缓存。
            "messages": _assemble_messages(messages),
        }
        assembled_system = _assemble_system(system)
        if assembled_system:
            params["system"] = assembled_system
        assembled_tools = _assemble_tools(tools)
        if assembled_tools:
            params["tools"] = assembled_tools
        if self.config.thinking.enabled:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.config.thinking.budget_tokens,
            }
            # claude-3-7 requires the beta header; Claude 4+ does not
            if "3-7" in self.config.model:
                params["extra_headers"] = {"anthropic-beta": "thinking-2025-02-19"}

        # 流式拼接 tool_use 的参数 JSON 碎片：index -> {"id","name","json"}。
        tool_blocks: dict[int, dict] = {}
        text_parts: list[str] = []
        final_message = None
        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    cb = getattr(event, "content_block", None)
                    if cb is not None and getattr(cb, "type", None) == "tool_use":
                        tool_blocks[event.index] = {
                            "id": cb.id,
                            "name": cb.name,
                            "json": "",
                        }
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "thinking_delta":
                        yield StreamChunk(ChunkType.THINKING, delta.thinking)
                    elif delta.type == "text_delta":
                        text_parts.append(delta.text)
                        yield StreamChunk(ChunkType.TEXT, delta.text)
                    elif delta.type == "input_json_delta":
                        tb = tool_blocks.get(event.index)
                        if tb is not None:
                            tb["json"] += delta.partial_json
            final_message = await stream.get_final_message()

        # 组装完整工具调用（碎片拼接后解析 JSON）。
        tool_calls: list[dict] = []
        for idx in sorted(tool_blocks):
            tb = tool_blocks[idx]
            raw = tb["json"].strip()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {}
            tool_calls.append({"id": tb["id"], "name": tb["name"], "input": parsed})

        # assistant 内容块：thinking/text 维持既有产出；有工具调用时追加 tool_use 块供回灌。
        blocks: list | None = None
        if (self.config.thinking.enabled and final_message) or tool_calls:
            blocks = []
            if self.config.thinking.enabled and final_message:
                for block in final_message.content:
                    if block.type == "thinking":
                        blocks.append({
                            "type": "thinking",
                            "thinking": block.thinking,
                            "signature": getattr(block, "signature", ""),
                        })
                    elif block.type == "text":
                        blocks.append({"type": "text", "text": block.text})
            else:
                text = "".join(text_parts)
                if text:
                    blocks.append({"type": "text", "text": text})
            for tc in tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["input"],
                })

        # 缓存可观测（#0006 T6）：读 usage 缓存字段，记录日志；端点未回传则降级、不抛错。
        cache_read, cache_creation = self._read_cache_usage(final_message)

        # DONE 透出 stop_reason（取自流式最终消息）+ 缓存字段，供循环判断与可观测。
        yield StreamChunk(
            ChunkType.DONE,
            blocks=blocks,
            tool_calls=tool_calls or None,
            stop_reason=getattr(final_message, "stop_reason", None),
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        )

    @staticmethod
    def _read_cache_usage(final_message) -> tuple[int | None, int | None]:
        """从最终消息 usage 读取缓存命中 / 创建字段；缺失则记一条降级日志、返回 (None, None)。"""
        usage = getattr(final_message, "usage", None)
        cache_read = getattr(usage, "cache_read_input_tokens", None) if usage else None
        cache_creation = getattr(usage, "cache_creation_input_tokens", None) if usage else None
        if cache_read is None and cache_creation is None:
            logger.info("端点未回传缓存字段")
        else:
            logger.info(
                "缓存命中 cache_read_input_tokens=%s，创建 cache_creation_input_tokens=%s",
                cache_read,
                cache_creation,
            )
        return cache_read, cache_creation
