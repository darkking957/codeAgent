from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum


class ChunkType(Enum):
    # provider 原生流式类型（#0018 起，引擎循环信号已移出本枚举，归 coreagent.events）。
    THINKING = "thinking"
    TEXT = "text"
    DONE = "done"


@dataclass
class StreamChunk:
    type: ChunkType
    content: str = ""
    # For DONE chunks from Anthropic with thinking / tool use: full assistant content
    # blocks (含 tool_use)，供回灌多轮历史。
    blocks: list | None = None
    # For DONE chunks: 解析后的工具调用列表 [{"id","name","input"}, ...]，供按名分派。
    tool_calls: list | None = None
    # DONE 携带：模型本轮停止原因（end_turn / tool_use / max_tokens / pause_turn …），provider 原生字段。
    stop_reason: str | None = None
    # ── 服务端工具块（#0024 web_search）；DONE 携带，由引擎映射到既有 ToolCall/ToolResultEvent ──
    # server_tool_calls：[{"id","name","input"}, ...]，对应响应中的 server_tool_use 块（服务端已执行）。
    # server_tool_results：[{"tool_use_id","content","is_error"}, ...]，对应 web_search_tool_result 块。
    # 二者均非客户端工具（不经 ToolRegistry 执行）；端点不回传（如普通对话）则为 None。
    server_tool_calls: list | None = None
    server_tool_results: list | None = None
    # ── 缓存可观测（#0006 T6）；DONE 携带 Anthropic usage 缓存字段，端点未回传则为 None ──
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    # ── 服务端 usage 输入/输出计数（#0009 T2）；DONE 携带，端点未回传（如 openai）则为 None ──
    input_tokens: int | None = None
    output_tokens: int | None = None


class BaseProvider(ABC):
    # 注：声明为返回 AsyncIterator 的普通方法（而非 async def），这是抽象异步生成器的
    # 惯用签名——实现仍是 async def + yield，调用方仍 `async for`，运行时契约不变。
    @abstractmethod
    def stream_chat(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        model_override: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        # ``model_override``：本次请求改用的模型（#0012 独立模式技能可声明专属模型）；
        # 为空走 config.model（共享模式与既有调用行为不变）。
        ...
