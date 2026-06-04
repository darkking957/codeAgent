from collections.abc import AsyncIterator

import anthropic as _anthropic

from coreagent.config import Config
from coreagent.providers.base import BaseProvider, ChunkType, StreamChunk


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
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        max_tokens = (
            self.config.thinking_max_tokens
            if self.config.thinking.enabled
            else self.config.max_tokens
        )
        params: dict = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            params["system"] = system
        if self.config.thinking.enabled:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.config.thinking.budget_tokens,
            }
            # claude-3-7 requires the beta header; Claude 4+ does not
            if "3-7" in self.config.model:
                params["extra_headers"] = {"anthropic-beta": "thinking-2025-02-19"}

        final_message = None
        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "thinking_delta":
                        yield StreamChunk(ChunkType.THINKING, delta.thinking)
                    elif delta.type == "text_delta":
                        yield StreamChunk(ChunkType.TEXT, delta.text)
            final_message = await stream.get_final_message()

        blocks: list | None = None
        if self.config.thinking.enabled and final_message:
            blocks = []
            for block in final_message.content:
                if block.type == "thinking":
                    blocks.append({
                        "type": "thinking",
                        "thinking": block.thinking,
                        "signature": getattr(block, "signature", ""),
                    })
                elif block.type == "text":
                    blocks.append({"type": "text", "text": block.text})

        yield StreamChunk(ChunkType.DONE, blocks=blocks)
