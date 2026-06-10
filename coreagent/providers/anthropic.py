import json
import logging
from collections.abc import AsyncIterator

import anthropic as _anthropic

from coreagent.agents.constants import BACKGROUND_RESULTS_TAG_OPEN
from coreagent.config import Config
from coreagent.environment import ENV_TAG_OPEN
from coreagent.injection import REMINDER_OPEN
from coreagent.memory.constants import INSTRUCTIONS_TAG_OPEN
from coreagent.providers.base import BaseProvider, ChunkType, StreamChunk
from coreagent.skills.constants import CATALOG_TAG_OPEN
from coreagent.teams.constants import INBOX_TAG_OPEN

logger = logging.getLogger(__name__)

_CACHE_CONTROL = {"type": "ephemeral"}


# ── 缓存通道装配（#0006 T4/T5）：把稳定内容放可缓存前缀、变化内容放断点之后 ──────────────

def _assemble_system(system: str | list | None) -> list | None:
    """把 `system` 组成块列表：稳定模块块打 cache_control、session-specific 块不打。

    不打缓存的 session-specific 块：环境快照块（含 <env>）、项目指令块（含
    <project-instructions>，其项目/本地层随 cwd 变）、技能目录块（含 <available-skills>）、
    后台子 Agent 结果后缀块（含 <background-agent-results>，#0014，每轮 drain 即变）、团队邮箱后缀块
    （含 <team-inbox>，#0016，每轮 drain 即变）。向后兼容：纯字符串 → 单个稳定块（打缓存）。
    """
    if not system:
        return None
    items = [system] if isinstance(system, str) else system
    blocks: list[dict] = []
    for item in items:
        text = item if isinstance(item, str) else item.get("text", "")
        block: dict = {"type": "text", "text": text}
        # 环境块 / 项目指令块 / 技能目录块 / 后台结果后缀块 / 团队邮箱后缀块是 session-specific，
        # 绝不进可缓存前缀（否则破坏跨会话/跨轮命中）。
        if (
            ENV_TAG_OPEN not in text
            and INSTRUCTIONS_TAG_OPEN not in text
            and CATALOG_TAG_OPEN not in text
            and BACKGROUND_RESULTS_TAG_OPEN not in text
            and INBOX_TAG_OPEN not in text
        ):
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


# ── 服务端工具块解析（#0024 web_search）：从最终消息抽取 server_tool_use / web_search_tool_result ──
# 这些块由服务端执行（非本仓 ToolRegistry），按文档字段忠实转 dict——既供引擎映射到既有事件，
# 也供历史回灌 / pause_turn 续跑（encrypted_content / citations 须原样保留）。读属性而非 model_dump：
# 同时兼容真 SDK pydantic 块与单测的 SimpleNamespace 假块，且只取「输入兼容」字段（避免回灌多余字段被端点 400）。

def _citation_to_dict(c) -> dict:
    """text 块的 web_search 引用 → dict（cited_text / url / title / encrypted_index）。"""
    return {
        "type": getattr(c, "type", "web_search_result_location"),
        "url": getattr(c, "url", ""),
        "title": getattr(c, "title", None),
        "cited_text": getattr(c, "cited_text", ""),
        "encrypted_index": getattr(c, "encrypted_index", ""),
    }


def _search_result_content_to_dict(content):
    """web_search_tool_result 的 content → dict：结果列表（含 encrypted_content）或错误对象。"""
    if isinstance(content, list):
        return [
            {
                "type": getattr(r, "type", "web_search_result"),
                "title": getattr(r, "title", ""),
                "url": getattr(r, "url", ""),
                "encrypted_content": getattr(r, "encrypted_content", ""),
                "page_age": getattr(r, "page_age", None),
            }
            for r in content
        ]
    # 错误对象（WebSearchToolResultError）：保留 error_code 供前端 / 历史区分降级。
    return {
        "type": getattr(content, "type", "web_search_tool_result_error"),
        "error_code": getattr(content, "error_code", ""),
    }


def _block_to_dict(block) -> dict:
    """单个内容块 → 输入兼容 dict（按 type 抽取，兼容真 SDK 块与 SimpleNamespace 假块）。"""
    btype = getattr(block, "type", None)
    if btype == "text":
        d: dict = {"type": "text", "text": getattr(block, "text", "")}
        cits = getattr(block, "citations", None)
        if cits:
            d["citations"] = [_citation_to_dict(c) for c in cits]
        return d
    if btype == "thinking":
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", ""),
            "signature": getattr(block, "signature", ""),
        }
    if btype == "server_tool_use":
        return {
            "type": "server_tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", "web_search"),
            "input": getattr(block, "input", {}) or {},
        }
    if btype == "web_search_tool_result":
        return {
            "type": "web_search_tool_result",
            "tool_use_id": getattr(block, "tool_use_id", ""),
            "content": _search_result_content_to_dict(getattr(block, "content", [])),
        }
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}) or {},
        }
    return {"type": btype or "text"}


def _extract_server_tools(final_message) -> tuple[list[dict], list[dict]]:
    """从最终消息抽取服务端工具调用 / 结果块（供 DONE 透出给引擎映射事件）。

    返回 (server_tool_calls, server_tool_results)：
    - server_tool_calls：[{"id","name","input"}, ...]，对应 server_tool_use 块；
    - server_tool_results：[{"tool_use_id","content","is_error"}, ...]，对应 web_search_tool_result 块，
      ``is_error`` 据 content 是否为错误对象判定（供前端 / 历史区分降级）。
    无服务端工具（普通对话）→ 返回两个空列表。
    """
    calls: list[dict] = []
    results: list[dict] = []
    content = getattr(final_message, "content", None) or []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "server_tool_use":
            calls.append({
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", "web_search"),
                "input": getattr(block, "input", {}) or {},
            })
        elif btype == "web_search_tool_result":
            result_content = _search_result_content_to_dict(getattr(block, "content", []))
            is_error = isinstance(result_content, dict) and result_content.get("type", "").endswith("error")
            results.append({
                "tool_use_id": getattr(block, "tool_use_id", ""),
                "content": result_content,
                "is_error": is_error,
            })
    return calls, results


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
        model_override: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        max_tokens = (
            self.config.thinking_max_tokens
            if self.config.thinking.enabled
            else self.config.max_tokens
        )
        params: dict = {
            # 独立模式技能可声明专属模型（#0012）：model_override 非空即用之，否则走 config.model。
            "model": model_override or self.config.model,
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
            # claude-3-7 requires the beta header; Claude 4+ does not（按实际请求模型判定）
            if "3-7" in (model_override or self.config.model):
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

        # 服务端工具块（#0024 web_search）：从最终消息抽取 server_tool_use / web_search_tool_result，
        # 供 DONE 透出给引擎映射到既有 ToolCall/ToolResultEvent（不经 ToolRegistry 执行）。
        server_tool_calls, server_tool_results = _extract_server_tools(final_message)
        has_server_tools = bool(server_tool_calls or server_tool_results)

        # assistant 内容块：thinking/text 维持既有产出；有工具调用时追加 tool_use 块供回灌。
        blocks: list | None = None
        if has_server_tools and final_message is not None:
            # 服务端工具轮：忠实序列化**全部**内容块（text+citations / server_tool_use /
            # web_search_tool_result / 任何 tool_use），供历史回灌与 pause_turn 续跑——encrypted_content
            # 与 citations 必须原样保留，否则续跑 / 引用定位会失效。
            blocks = [_block_to_dict(b) for b in final_message.content]
        elif (self.config.thinking.enabled and final_message) or tool_calls:
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
        # 服务端 usage 输入/输出计数（#0009 T2）：供上下文管理锚定实时 token 估算；缺失为 None。
        input_tokens, output_tokens = self._read_io_usage(final_message)

        # DONE 透出 stop_reason（取自流式最终消息）+ 缓存字段 + usage 输入/输出，供循环判断与上下文管理；
        # 服务端工具调用 / 结果（#0024）一并透出，供引擎映射到既有事件并据 pause_turn 续跑。
        yield StreamChunk(
            ChunkType.DONE,
            blocks=blocks,
            tool_calls=tool_calls or None,
            stop_reason=getattr(final_message, "stop_reason", None),
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            server_tool_calls=server_tool_calls or None,
            server_tool_results=server_tool_results or None,
        )

    @staticmethod
    def _read_io_usage(final_message) -> tuple[int | None, int | None]:
        """从最终消息 usage 读取服务端输入/输出 token 计数；缺失则返回 (None, None)。"""
        usage = getattr(final_message, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage else None
        output_tokens = getattr(usage, "output_tokens", None) if usage else None
        return input_tokens, output_tokens

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
