from collections.abc import AsyncIterator
from typing import cast

import openai as _openai
from openai.types.chat import ChatCompletionMessageParam

from coreagent.config import Config
from coreagent.providers.base import BaseProvider, ChunkType, StreamChunk


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
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        chat_messages: list[dict] = []
        if system:
            chat_messages.append({"role": "system", "content": system})
        chat_messages.extend(messages)

        # 我们内部用普通 dict 表示消息；在 SDK 边界 cast 为其 TypedDict 入参类型。
        # 同时 stream=True（字面量）让重载解析为 AsyncStream，可直接 async for 迭代。
        stream = await self.client.chat.completions.create(
            model=self.config.model,
            messages=cast(list[ChatCompletionMessageParam], chat_messages),
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield StreamChunk(ChunkType.TEXT, chunk.choices[0].delta.content)

        yield StreamChunk(ChunkType.DONE)
