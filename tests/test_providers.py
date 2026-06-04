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
