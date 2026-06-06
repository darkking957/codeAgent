from types import SimpleNamespace

import pytest

from coreagent.providers import create_provider
from coreagent.providers.anthropic import AnthropicProvider
from coreagent.providers.base import ChunkType
from coreagent.providers.openai import OpenAIProvider

from .conftest import make_config

# ── create_provider 选型 ─────────────────────────────────────────────────────────

def test_create_provider_anthropic():
    assert isinstance(create_provider(make_config(protocol="anthropic")), AnthropicProvider)


def test_create_provider_openai():
    assert isinstance(create_provider(make_config(protocol="openai")), OpenAIProvider)


def test_create_provider_unknown():
    with pytest.raises(ValueError) as ei:
        create_provider(make_config(protocol="grpc"))
    assert "不支持" in str(ei.value)


# ── 假 SSE 事件流驱动 AnthropicProvider.stream_chat ───────────────────────────────

def _delta_event(delta_type: str, **kw):
    return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type=delta_type, **kw))


def _delta_event_idx(index: int, delta_type: str, **kw):
    return SimpleNamespace(
        type="content_block_delta", index=index, delta=SimpleNamespace(type=delta_type, **kw)
    )


def _block_start(index: int, **kw):
    return SimpleNamespace(
        type="content_block_start", index=index, content_block=SimpleNamespace(**kw)
    )


class _FakeStream:
    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()

    async def get_final_message(self):
        return self._final


async def test_anthropic_stream_order(monkeypatch):
    cfg = make_config(thinking=SimpleNamespace(enabled=True, budget_tokens=1000))
    provider = AnthropicProvider(cfg)

    events = [
        _delta_event("thinking_delta", thinking="let me "),
        _delta_event("thinking_delta", thinking="think"),
        _delta_event("text_delta", text="hello "),
        _delta_event("text_delta", text="world"),
    ]
    final = SimpleNamespace(content=[
        SimpleNamespace(type="thinking", thinking="let me think", signature="sig"),
        SimpleNamespace(type="text", text="hello world"),
    ])
    fake_stream = _FakeStream(events, final)
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: fake_stream))

    chunks = []
    async for c in provider.stream_chat([{"role": "user", "content": "hi"}]):
        chunks.append(c)

    types = [c.type for c in chunks]
    # 顺序：THINKING… → TEXT… → DONE
    assert types[0] == ChunkType.THINKING
    assert ChunkType.TEXT in types
    assert types[-1] == ChunkType.DONE
    # THINKING 全部在 TEXT 之前，TEXT 全部在 DONE 之前
    last_think = max(i for i, t in enumerate(types) if t == ChunkType.THINKING)
    first_text = min(i for i, t in enumerate(types) if t == ChunkType.TEXT)
    assert last_think < first_text
    # DONE 携带内容块（thinking + text）
    assert chunks[-1].blocks is not None
    assert chunks[-1].blocks[0]["type"] == "thinking"
    assert chunks[-1].blocks[-1]["type"] == "text"


async def test_anthropic_assembles_streamed_tool_use(monkeypatch):
    """分片的 tool_use 参数 JSON 碎片 → provider 拼接后得到完整工具调用。"""
    cfg = make_config()
    provider = AnthropicProvider(cfg)

    events = [
        _block_start(0, type="tool_use", id="tu_1", name="read_file", input={}),
        _delta_event_idx(0, "input_json_delta", partial_json='{"pa'),
        _delta_event_idx(0, "input_json_delta", partial_json='th": "REA'),
        _delta_event_idx(0, "input_json_delta", partial_json='DME.md"}'),
    ]
    # final_message 不含已组装 input，确保断言的是 provider 自身的碎片拼接结果。
    final = SimpleNamespace(content=[])
    fake_stream = _FakeStream(events, final)
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: fake_stream))

    chunks = []
    async for c in provider.stream_chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"name": "read_file", "description": "d", "input_schema": {}}],
    ):
        chunks.append(c)

    done = chunks[-1]
    assert done.type == ChunkType.DONE
    # 组装出的工具名与参数 dict 正确
    assert done.tool_calls == [
        {"id": "tu_1", "name": "read_file", "input": {"path": "README.md"}}
    ]
    # assistant 内容块含 tool_use 块（供回灌多轮）
    assert done.blocks is not None
    assert any(b["type"] == "tool_use" and b["name"] == "read_file" for b in done.blocks)


def test_stream_chat_signature_has_tools():
    import inspect

    from coreagent.providers.base import BaseProvider

    sig = inspect.signature(BaseProvider.stream_chat)
    assert "tools" in sig.parameters


# ── DONE 透出 stop_reason（#0004 T1）─────────────────────────────────────────────

async def test_anthropic_done_carries_stop_reason_plain():
    """不带工具的纯对话：DONE 仍带 stop_reason（取自 final_message）。"""
    cfg = make_config()
    provider = AnthropicProvider(cfg)
    events = [_delta_event("text_delta", text="hi")]
    final = SimpleNamespace(content=[], stop_reason="end_turn")
    fake_stream = _FakeStream(events, final)
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: fake_stream))

    chunks = []
    async for c in provider.stream_chat([{"role": "user", "content": "hi"}]):
        chunks.append(c)

    assert chunks[-1].type == ChunkType.DONE
    assert chunks[-1].stop_reason == "end_turn"


async def test_anthropic_done_carries_stop_reason_with_tools():
    """带工具：DONE 透出 stop_reason（通常为 tool_use）。"""
    cfg = make_config()
    provider = AnthropicProvider(cfg)
    events = [
        _block_start(0, type="tool_use", id="tu_1", name="read_file", input={}),
        _delta_event_idx(0, "input_json_delta", partial_json='{"path": "a"}'),
    ]
    final = SimpleNamespace(content=[], stop_reason="tool_use")
    fake_stream = _FakeStream(events, final)
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: fake_stream))

    chunks = []
    async for c in provider.stream_chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"name": "read_file", "description": "d", "input_schema": {}}],
    ):
        chunks.append(c)

    done = chunks[-1]
    assert done.type == ChunkType.DONE
    assert done.stop_reason == "tool_use"
    assert done.tool_calls  # 仍正常解析工具调用


def test_chunktype_has_loop_signals():
    """ChunkType 含四个循环级信号。"""
    names = {m.name for m in ChunkType}
    assert {"TURN_START", "TURN_END", "LOOP_DONE", "LOOP_ERROR"} <= names


# ── #0006 T4/T5：缓存断点装配（结构验收，单测纯函数）─────────────────────────────────

from coreagent.environment import ENV_TAG_OPEN, ENV_TAG_CLOSE
from coreagent.injection import REMINDER_OPEN, REMINDER_CLOSE
from coreagent.providers.anthropic import (
    _assemble_messages,
    _assemble_system,
    _assemble_tools,
)


def _env_block() -> str:
    return f"{ENV_TAG_OPEN}\ncwd: /x\nos: y\ndate: z\ngit: g\n{ENV_TAG_CLOSE}"


def _count_cache_control(obj) -> int:
    """递归统计结构里 cache_control 断点数。"""
    n = 0
    if isinstance(obj, dict):
        if "cache_control" in obj:
            n += 1
        for v in obj.values():
            n += _count_cache_control(v)
    elif isinstance(obj, list):
        for v in obj:
            n += _count_cache_control(v)
    return n


def test_system_stable_block_cached_env_block_not():
    """system 为块列表：首块（稳定）带 cache_control、尾块（环境）不带。"""
    blocks = _assemble_system(["稳定模块文本", _env_block()])
    assert blocks[0]["text"] == "稳定模块文本"
    assert "cache_control" in blocks[0]
    assert ENV_TAG_OPEN in blocks[-1]["text"]
    assert "cache_control" not in blocks[-1]


def test_system_plain_string_backward_compatible():
    """纯字符串 system 仍可用：单个稳定块、带 cache_control。"""
    blocks = _assemble_system("纯字符串系统")
    assert len(blocks) == 1
    assert blocks[0]["text"] == "纯字符串系统"
    assert "cache_control" in blocks[0]


def test_tools_last_block_cached():
    tools = [
        {"name": "a", "description": "d", "input_schema": {}},
        {"name": "b", "description": "d", "input_schema": {}},
    ]
    out = _assemble_tools(tools)
    assert "cache_control" not in out[0]
    assert "cache_control" in out[-1]


def test_messages_last_finalized_gets_rolling_cache():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]
    out = _assemble_messages(msgs)
    # 末条已定型消息（assistant "yo"）末块带滚动 cache_control
    assert "cache_control" in out[-1]["content"][-1]
    # 前一条不带
    assert _count_cache_control(out[0]) == 0


def test_reminder_after_rolling_breakpoint_and_uncached():
    reminder = f"{REMINDER_OPEN}\n仍在 plan 模式\n{REMINDER_CLOSE}"
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": reminder},
    ]
    out = _assemble_messages(msgs)
    # 滚动断点落在「最后一条已定型消息」= 非提醒的 "hi"
    rolling_idx = next(i for i, m in enumerate(out) if _count_cache_control(m) > 0)
    reminder_idx = next(i for i, m in enumerate(out)
                        if isinstance(m["content"], str) and REMINDER_OPEN in m["content"])
    assert reminder_idx > rolling_idx          # 提醒在滚动断点之后
    # 提醒消息本身不带 cache_control（仍是字符串内容，未被转块）
    assert _count_cache_control(out[reminder_idx]) == 0


def test_total_cache_breakpoints_within_limit():
    """全请求（tools + system + messages）断点总数 ≤ 4。"""
    system = _assemble_system(["稳定", _env_block()])
    tools = _assemble_tools([{"name": "a", "description": "d", "input_schema": {}}])
    messages = _assemble_messages([
        {"role": "user", "content": "hi"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}]},
    ])
    total = _count_cache_control(system) + _count_cache_control(tools) + _count_cache_control(messages)
    assert total <= 4


# ── #0006 T4：Anthropic 请求实际 params 落位（捕获 stream 入参）──────────────────────

async def test_anthropic_request_params_carry_cache_control():
    cfg = make_config()
    provider = AnthropicProvider(cfg)
    captured = {}

    def capture_stream(**kw):
        captured.update(kw)
        return _FakeStream([_delta_event("text_delta", text="ok")],
                           SimpleNamespace(content=[], stop_reason="end_turn"))

    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=capture_stream))

    async for _ in provider.stream_chat(
        [{"role": "user", "content": "hi"}],
        system=["稳定模块", _env_block()],
        tools=[{"name": "a", "description": "d", "input_schema": {}}],
    ):
        pass

    sys_blocks = captured["system"]
    assert "cache_control" in sys_blocks[0] and "cache_control" not in sys_blocks[-1]
    assert "cache_control" in captured["tools"][-1]
    assert "cache_control" in captured["messages"][-1]["content"][-1]


# ── #0006 T6：缓存可观测 ────────────────────────────────────────────────────────────

async def test_cache_usage_exposed_on_done(caplog):
    import logging
    cfg = make_config()
    provider = AnthropicProvider(cfg)
    final = SimpleNamespace(
        content=[], stop_reason="end_turn",
        usage=SimpleNamespace(cache_read_input_tokens=128, cache_creation_input_tokens=0),
    )
    fake = _FakeStream([_delta_event("text_delta", text="hi")], final)
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: fake))

    with caplog.at_level(logging.INFO, logger="coreagent.providers.anthropic"):
        chunks = []
        async for c in provider.stream_chat([{"role": "user", "content": "hi"}]):
            chunks.append(c)

    assert chunks[-1].cache_read_input_tokens == 128
    assert "cache_read_input_tokens=128" in caplog.text


async def test_cache_usage_missing_degrades(caplog):
    import logging
    cfg = make_config()
    provider = AnthropicProvider(cfg)
    final = SimpleNamespace(content=[], stop_reason="end_turn")  # 无 usage
    fake = _FakeStream([_delta_event("text_delta", text="hi")], final)
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: fake))

    with caplog.at_level(logging.INFO, logger="coreagent.providers.anthropic"):
        chunks = []
        async for c in provider.stream_chat([{"role": "user", "content": "hi"}]):
            chunks.append(c)

    assert chunks[-1].cache_read_input_tokens is None
    assert "端点未回传缓存字段" in caplog.text


# ── #0006 T4：OpenAI 行为不变（单条 system 字符串、无 cache_control、忽略 tools）──────

class _FakeOpenAIStream:
    def __aiter__(self):
        async def gen():
            return
            yield  # pragma: no cover
        return gen()


async def test_openai_flattens_system_no_cache_no_tools():
    cfg = make_config(protocol="openai")
    provider = OpenAIProvider(cfg)
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return _FakeOpenAIStream()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    async for _ in provider.stream_chat(
        [{"role": "user", "content": "hi"}],
        system=["稳定模块", _env_block()],
        tools=[{"name": "a", "description": "d", "input_schema": {}}],
    ):
        pass

    # system 拍平为单条字符串（含两块文本拼接），不携带 cache_control / 结构化块
    sys_msg = captured["messages"][0]
    assert sys_msg["role"] == "system"
    assert isinstance(sys_msg["content"], str)
    assert "稳定模块" in sys_msg["content"] and ENV_TAG_OPEN in sys_msg["content"]
    # 全请求无 cache_control；tools 被忽略（未进 create 入参）
    assert _count_cache_control(captured) == 0
    assert "tools" not in captured
