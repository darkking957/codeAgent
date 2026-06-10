from collections.abc import AsyncIterator
from typing import cast

import openai as _openai
from openai.types.chat import ChatCompletionMessageParam

from coreagent.config import Config
from coreagent.providers.base import BaseProvider, ChunkType, StreamChunk


def _flatten_system(system: str | list | None) -> str:
    """把结构化 system 块拍平回单字符串（丢 cache_control）；纯字符串原样返回。"""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    texts = [item if isinstance(item, str) else item.get("text", "") for item in system]
    return "\n\n".join(t for t in texts if t)


class OpenAIProvider(BaseProvider):
    def __init__(self, config: Config) -> None:
        self.config = config
        kwargs: dict = {"api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = _openai.AsyncOpenAI(**kwargs)

    async def stream_chat(
        self,
        messages: list[dict],
        system: str | list | None = None,
        tools: list[dict] | None = None,
        model_override: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        # OpenAI 行为不变（#0006）：仍以单条 system 字符串发送、忽略 tools、不做缓存。
        # system 若为结构化块（#0006 T4）则拍平回单字符串、丢弃 cache_control。
        chat_messages: list[dict] = []
        system_text = _flatten_system(system)
        if system_text:
            chat_messages.append({"role": "system", "content": system_text})
        chat_messages.extend(messages)

        # 我们内部用普通 dict 表示消息；在 SDK 边界 cast 为其 TypedDict 入参类型。
        # 同时 stream=True（字面量）让重载解析为 AsyncStream，可直接 async for 迭代。
        stream = await self.client.chat.completions.create(
            model=model_override or self.config.model,
            messages=cast(list[ChatCompletionMessageParam], chat_messages),
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield StreamChunk(ChunkType.TEXT, chunk.choices[0].delta.content)

        yield StreamChunk(ChunkType.DONE)
