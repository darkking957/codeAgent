import openai as _openai
from coreagent.providers.base import BaseProvider, ChunkType, StreamChunk


class OpenAIProvider(BaseProvider):
    def __init__(self, config):
        self.config = config
        kwargs = {"api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = _openai.AsyncOpenAI(**kwargs)

    async def stream_chat(self, messages, system=None):
        chat_messages = []
        if system:
            chat_messages.append({"role": "system", "content": system})
        chat_messages.extend(messages)

        stream = await self.client.chat.completions.create(
            model=self.config.model,
            messages=chat_messages,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield StreamChunk(ChunkType.TEXT, chunk.choices[0].delta.content)

        yield StreamChunk(ChunkType.DONE)
