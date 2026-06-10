"""测试公用夹具与假 provider（全部离线，不触网、不需要 API key）。"""

from types import SimpleNamespace

from coreagent.providers.base import BaseProvider, ChunkType, StreamChunk


class FakeStatusError(Exception):
    """模拟带 HTTP 状态码的 SDK 异常（用于重试分类测试）。"""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


class ScriptedProvider(BaseProvider):
    """按脚本回放的假 provider：每次 stream_chat 消费一段脚本。

    `attempts` 为列表，每项是若干 item：
      - StreamChunk → yield；
      - Exception   → raise。
    调用次数超出脚本长度时复用最后一段（便于"每次都抛同一错误"）。
    """

    def __init__(self, attempts: list[list]) -> None:
        self._attempts = list(attempts)
        self.calls = 0
        # 记录每次调用收到的 tools / system / messages 入参，便于断言注入与装配。
        self.tools_seen: list = []
        self.system_seen: list = []
        self.messages_seen: list = []

    async def stream_chat(self, messages, system=None, tools=None):
        idx = self.calls if self.calls < len(self._attempts) else len(self._attempts) - 1
        entry = self._attempts[idx]
        self.calls += 1
        self.tools_seen.append(tools)
        self.system_seen.append(system)
        self.messages_seen.append(messages)
        for item in entry:
            if isinstance(item, BaseException):
                raise item
            yield item


def text_chunk(s: str) -> StreamChunk:
    return StreamChunk(ChunkType.TEXT, s)


def done_chunk(blocks=None, tool_calls=None, stop_reason=None) -> StreamChunk:
    return StreamChunk(
        ChunkType.DONE, blocks=blocks, tool_calls=tool_calls, stop_reason=stop_reason
    )


def tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    """构造一条工具调用（与 provider DONE.tool_calls 同构）。"""
    return {"id": call_id, "name": name, "input": arguments}


def tool_use_done(
    name: str,
    arguments: dict,
    call_id: str = "call_1",
    text: str = "",
    stop_reason: str = "tool_use",
) -> StreamChunk:
    """构造一个携带工具调用的 DONE chunk：含 assistant 内容块 + 解析后的工具调用。"""
    blocks: list = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
    return StreamChunk(
        ChunkType.DONE,
        blocks=blocks,
        tool_calls=[tool_call(name, arguments, call_id)],
        stop_reason=stop_reason,
    )


def tools_done(tool_calls: list[dict], text: str = "", stop_reason: str = "tool_use") -> StreamChunk:
    """构造一个携带多个工具调用的 DONE chunk（同一轮多工具，供并发/分类测试）。"""
    blocks: list = []
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in tool_calls:
        blocks.append(
            {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
        )
    return StreamChunk(
        ChunkType.DONE,
        blocks=blocks,
        tool_calls=list(tool_calls),
        stop_reason=stop_reason,
    )


def make_config(**overrides) -> SimpleNamespace:
    """构造 provider 够用的轻量配置（绕过 pydantic，便于设非法 protocol 等）。"""
    base = dict(
        protocol="anthropic",
        model="claude-test",
        api_key="test-key",
        base_url="",
        thinking=SimpleNamespace(enabled=False, budget_tokens=10000),
        max_tokens=8192,
        thinking_max_tokens=16000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)
