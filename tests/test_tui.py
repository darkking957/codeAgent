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


async def test_main_prompt_message_reset_each_iteration(monkeypatch):
    """主输入框提示符不被 confirm/plan 的临时 message 污染：主循环每次显式传回 _PROMPT_MSG。

    回归：prompt_toolkit 的 prompt_async(message=...) 会 `self.message = message`，
    confirm（⚠ 执行…? [y/N]）复用同一 session 会改掉主提示符；修复前主循环不传 message，
    残留确认文案。本用例断言主循环每次取输入都显式带上 _PROMPT_MSG。
    """
    from coreagent.tui import _PROMPT_MSG

    tui, _ = _make_tui(ScriptedProvider([[done_chunk()]]), monkeypatch)
    seen: list = []
    inputs = iter(["/help", "/exit"])

    async def fake_prompt(*a, message=None, **k):
        seen.append(message)
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(tui.session, "prompt_async", fake_prompt)
    await asyncio.wait_for(tui.run(), timeout=10)

    assert seen  # 主循环确有取输入
    assert all(m is _PROMPT_MSG for m in seen)  # 每次都显式复位（修复前为 None → 残留确认文案）


async def test_full_flow_help_clear_exit(monkeypatch, capsys):
    tui, conv = _make_tui(ScriptedProvider([[done_chunk()]]), monkeypatch)
    _feed(tui, monkeypatch, ["/help", "/clear", "/exit"])

    await asyncio.wait_for(tui.run(), timeout=10)  # 不应抛异常

    out = capsys.readouterr().out
    assert "/help" in out  # 横幅 + help 输出


# ── #0005 富交互确认接入 + 本会话信任集（T5）────────────────────────────────
def _wc(name, **inp):
    return {"id": "x", "name": name, "input": inp}


async def test_trusted_tool_skips_menu_second_time(monkeypatch):
    """选「同意且记住」后，同会话内同一工具二次写调用直接放行、不再弹菜单。"""
    from coreagent import confirm_ui

    tui, _ = _make_tui(ScriptedProvider([[done_chunk()]]), monkeypatch)
    monkeypatch.setattr(confirm_ui, "is_interactive", lambda: True)
    seen: list = []

    async def fake_menu(preview, **k):
        seen.append(preview.tool_name)
        return confirm_ui.APPROVE_ALWAYS

    monkeypatch.setattr(confirm_ui, "confirm_interactive", fake_menu)

    assert await tui._confirm(_wc("run_command", command="ls")) is True
    assert "run_command" in tui._trusted
    assert await tui._confirm(_wc("run_command", command="pwd")) is True  # 二次直接放行
    assert seen == ["run_command"]  # 菜单只弹过一次


async def test_trust_isolated_by_name(monkeypatch):
    """信任按名隔离：记住 run_command 后，edit_file 仍弹菜单。"""
    from coreagent import confirm_ui

    tui, _ = _make_tui(ScriptedProvider([[done_chunk()]]), monkeypatch)
    monkeypatch.setattr(confirm_ui, "is_interactive", lambda: True)
    seen: list = []

    async def fake_menu(preview, **k):
        seen.append(preview.tool_name)
        return (
            confirm_ui.APPROVE_ALWAYS
            if preview.tool_name == "run_command"
            else confirm_ui.REJECT
        )

    monkeypatch.setattr(confirm_ui, "confirm_interactive", fake_menu)

    await tui._confirm(_wc("run_command", command="ls"))
    res = await tui._confirm(_wc("edit_file", path="a", old_string="x", new_string="y"))
    assert res is False
    assert seen == ["run_command", "edit_file"]  # edit_file 未被信任、仍弹菜单


async def test_non_tty_degrades_no_menu_no_trust(monkeypatch):
    """非 TTY → 走纯文本降级；不渲染富菜单、不写信任集。"""
    from coreagent import confirm_ui

    tui, _ = _make_tui(ScriptedProvider([[done_chunk()]]), monkeypatch)
    monkeypatch.setattr(confirm_ui, "is_interactive", lambda: False)
    called = {"menu": False}

    async def fake_menu(preview, **k):
        called["menu"] = True
        return confirm_ui.APPROVE

    async def fake_plain(name, **k):
        return False

    monkeypatch.setattr(confirm_ui, "confirm_interactive", fake_menu)
    monkeypatch.setattr(confirm_ui, "confirm_plain", fake_plain)

    res = await tui._confirm(_wc("write_file", path="a", content="b"))
    assert res is False
    assert called["menu"] is False  # 富菜单未渲染
    assert tui._trusted == set()    # 信任集仍为空


async def test_confirm_returns_bool_contract(monkeypatch):
    """契约不变：三态收敛为 bool（同意类→True、拒绝→False）。"""
    from coreagent import confirm_ui

    tui, _ = _make_tui(ScriptedProvider([[done_chunk()]]), monkeypatch)
    monkeypatch.setattr(confirm_ui, "is_interactive", lambda: True)

    async def approve(preview, **k):
        return confirm_ui.APPROVE

    async def reject(preview, **k):
        return confirm_ui.REJECT

    monkeypatch.setattr(confirm_ui, "confirm_interactive", approve)
    r1 = await tui._confirm(_wc("run_command", command="ls"))
    monkeypatch.setattr(confirm_ui, "confirm_interactive", reject)
    r2 = await tui._confirm(_wc("edit_file", path="a", old_string="x", new_string="y"))
    assert r1 is True and r2 is False
    assert isinstance(r1, bool) and isinstance(r2, bool)
    assert tui._trusted == set()  # 纯「同意」不写信任集（只有「同意且记住」才写）
