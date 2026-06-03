from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncGenerator


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
    @abstractmethod
    async def stream_chat(
        self,
        messages: list[dict],
        system: str | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        ...
