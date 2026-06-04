from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum


class ChunkType(Enum):
    THINKING = "thinking"
    TEXT = "text"
    DONE = "done"


@dataclass
class StreamChunk:
    type: ChunkType
    content: str = ""
    # For DONE chunks from Anthropic with thinking: full content blocks with signatures
    blocks: list | None = None


class BaseProvider(ABC):
    # 注：声明为返回 AsyncIterator 的普通方法（而非 async def），这是抽象异步生成器的
    # 惯用签名——实现仍是 async def + yield，调用方仍 `async for`，运行时契约不变。
    @abstractmethod
    def stream_chat(
        self,
        messages: list[dict],
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        ...
