import asyncio

import coreagent.retry as retry_mod
from coreagent.config import Config
from coreagent.conversation import Conversation
from coreagent.tui import TUI

from .conftest import FakeStatusError, ScriptedProvider, done_chunk


def _make_tui(provider, monkeypatch):
    cfg = Config(protocol="anthropic", model="claude-test", api_key="k")
    conv = Conversation()
    tui = TUI(provider, cfg, conv)
    # 重定向持久化，避免写真实 history.json
    monkeypatch.setattr(conv, "save", lambda *a, **k: None)
    return tui, conv


def _feed(tui, monkeypatch, inputs):
    it = iter(inputs)

    async def fake_prompt(*a, **k):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(tui.session, "prompt_async", fake_prompt)


def _patch_instant_sleep(monkeypatch):
    orig = asyncio.sleep

    async def fast_sleep(d):
        await orig(0)  # 仅让出事件循环，近乎瞬时

    monkeypatch.setattr(retry_mod.asyncio, "sleep", fast_sleep)


def test_banner_and_help_no_raise(monkeypatch):
    tui, _ = _make_tui(ScriptedProvider([[done_chunk()]]), monkeypatch)
    tui._print_banner()
    tui._print_help()


async def test_retry_path_rollback(monkeypatch, capsys):
    provider = ScriptedProvider([[FakeStatusError(503)]])  # 总是抛可重试错误
    tui, conv = _make_tui(provider, monkeypatch)
    _patch_instant_sleep(monkeypatch)
    _feed(tui, monkeypatch, ["please answer"])

    await asyncio.wait_for(tui.run(), timeout=10)

    out = capsys.readouterr().out
    assert "重试" in out          # 界面出现"第 N 次重试…"
    assert "✗" in out             # 最终错误以 ✗ 展示
    assert conv.messages == []    # 末条 user 消息被回滚，不污染历史
    assert provider.calls == 4    # 初次 + 3 次重试


async def test_full_flow_help_clear_exit(monkeypatch, capsys):
    tui, conv = _make_tui(ScriptedProvider([[done_chunk()]]), monkeypatch)
    _feed(tui, monkeypatch, ["/help", "/clear", "/exit"])

    await asyncio.wait_for(tui.run(), timeout=10)  # 不应抛异常

    out = capsys.readouterr().out
    assert "/help" in out  # 横幅 + help 输出
