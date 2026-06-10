from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum


class ChunkType(Enum):
    THINKING = "thinking"
    TEXT = "text"
    DONE = "done"
    # ── 循环级信号（#0004 Agent Loop）：让上层感知多轮循环边界，与单向推送的
    # thinking/text/done 同走推送流，但只在多轮场景产生可见效果。 ──
    TURN_START = "turn_start"   # 轮开始（带 round_index）
    TURN_END = "turn_end"       # 轮结束（带 stop_reason，区分模型主动结束与仍要继续）
    LOOP_DONE = "loop_done"     # 循环结束（带 exit_reason：自然 / 上限 / 取消）
    LOOP_ERROR = "loop_error"   # 循环错误（带 error_type，与单个工具失败区分）


@dataclass
class StreamChunk:
    type: ChunkType
    content: str = ""
    # For DONE chunks from Anthropic with thinking / tool use: full assistant content
    # blocks (含 tool_use)，供回灌多轮历史。
    blocks: list | None = None
    # For DONE chunks: 解析后的工具调用列表 [{"id","name","input"}, ...]，供按名分派。
    tool_calls: list | None = None
    # ── 循环级元信息（#0004）；默认 None，不破坏既有 DONE/TEXT 构造 ──
    # DONE / TURN_END 携带：模型本轮停止原因（end_turn / tool_use / max_tokens …）。
    stop_reason: str | None = None
    # TURN_START / TURN_END 携带：第几轮（从 1 起）。
    round_index: int | None = None
    # LOOP_DONE 携带：退出原因（natural / cap / cancelled）。
    exit_reason: str | None = None
    # LOOP_ERROR 携带：不可恢复异常的类型名。
    error_type: str | None = None
    # LOOP_DONE 携带：plan-only 模式下被拦截写类的计划项列表（结构化输出）。
    plan: list | None = None
    # ── 缓存可观测（#0006 T6）；DONE 携带 Anthropic usage 缓存字段，端点未回传则为 None ──
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


class BaseProvider(ABC):
    # 注：声明为返回 AsyncIterator 的普通方法（而非 async def），这是抽象异步生成器的
    # 惯用签名——实现仍是 async def + yield，调用方仍 `async for`，运行时契约不变。
    @abstractmethod
    def stream_chat(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        ...
