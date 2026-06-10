import pytest

import coreagent.retry as retry_mod
from coreagent.retry import _is_retryable, stream_with_retry

from .conftest import FakeStatusError, ScriptedProvider, done_chunk, text_chunk


async def _drain(agen, sink=None):
    out = [] if sink is None else sink
    async for c in agen:
        out.append(c)
    return out


def _patch_sleep(monkeypatch):
    waits: list[int] = []

    async def fake_sleep(d):
        waits.append(d)

    monkeypatch.setattr(retry_mod.asyncio, "sleep", fake_sleep)
    return waits


# ── _is_retryable 分类 ──────────────────────────────────────────────────────────

def test_is_retryable_classification():
    assert _is_retryable(ConnectionError("down"))
    assert _is_retryable(TimeoutError("slow"))
    assert _is_retryable(FakeStatusError(429))
    assert _is_retryable(FakeStatusError(500))
    assert _is_retryable(FakeStatusError(503))
    for code in (400, 401, 403, 404, 422):
        assert not _is_retryable(FakeStatusError(code)), code
    assert not _is_retryable(ValueError("nope"))


# ── 成功路径不重试 ────────────────────────────────────────────────────────────────

async def test_success_no_retry(monkeypatch):
    waits = _patch_sleep(monkeypatch)
    retries: list = []
    provider = ScriptedProvider([[text_chunk("a"), text_chunk("b"), done_chunk()]])

    out = await _drain(
        stream_with_retry(provider, [], on_retry=lambda a, w: retries.append((a, w)))
    )
    assert [c.content for c in out[:2]] == ["a", "b"]
    assert provider.calls == 1
    assert waits == []
    assert retries == []


# ── 可重试错误：退避 1/2/4，on_retry 依次 1/2/3，重试 3 次后上抛 ──────────────────────

@pytest.mark.parametrize("exc", [ConnectionError("c"), FakeStatusError(429), FakeStatusError(503)])
async def test_retryable_backoff(monkeypatch, exc):
    waits = _patch_sleep(monkeypatch)
    retries: list = []
    provider = ScriptedProvider([[exc]])

    with pytest.raises(type(exc)):
        await _drain(
            stream_with_retry(provider, [], on_retry=lambda a, w: retries.append((a, w)))
        )

    assert waits == [1, 2, 4]
    assert retries == [(1, 1), (2, 2), (3, 4)]
    assert provider.calls == 4  # 初次 + 3 次重试


# ── 不可重试错误：0 次重试，立即上抛 ───────────────────────────────────────────────

async def test_non_retryable_immediate(monkeypatch):
    waits = _patch_sleep(monkeypatch)
    retries: list = []
    provider = ScriptedProvider([[FakeStatusError(401)]])

    with pytest.raises(FakeStatusError):
        await _drain(
            stream_with_retry(provider, [], on_retry=lambda a, w: retries.append((a, w)))
        )

    assert provider.calls == 1
    assert waits == []
    assert retries == []


# ── 已输出守卫：先 yield 再抛 → 不重试，异常直接上抛 ──────────────────────────────────

async def test_already_yielded_no_retry(monkeypatch):
    waits = _patch_sleep(monkeypatch)
    retries: list = []
    provider = ScriptedProvider([[text_chunk("partial"), FakeStatusError(503)]])

    got: list = []
    with pytest.raises(FakeStatusError):
        async for c in stream_with_retry(
            provider, [], on_retry=lambda a, w: retries.append((a, w))
        ):
            got.append(c)

    assert [c.content for c in got] == ["partial"]  # 已流出内容
    assert provider.calls == 1  # 未重试
    assert waits == []
    assert retries == []
