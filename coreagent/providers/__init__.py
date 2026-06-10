from coreagent.providers.base import BaseProvider, ChunkType, StreamChunk


def create_provider(config) -> BaseProvider:
    if config.protocol == "anthropic":
        from coreagent.providers.anthropic import AnthropicProvider
        return AnthropicProvider(config)
    elif config.protocol == "openai":
        from coreagent.providers.openai import OpenAIProvider
        return OpenAIProvider(config)
    else:
        raise ValueError(f"不支持的 protocol：{config.protocol!r}")


__all__ = ["BaseProvider", "ChunkType", "StreamChunk", "create_provider"]
