"""#0005 富交互执行确认 —— 纯函数（T1 预览构建 / T2 六组件渲染）、交互应用（T3）、降级（T4）。"""

import asyncio
import io

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from coreagent import confirm_ui
from coreagent.confirm_ui import (
    APPROVE,
    APPROVE_ALWAYS,
    CURSOR_CHAR,
    MAX_LINE_WIDTH,
    REJECT,
    build_preview,
    confirm_interactive,
    confirm_plain,
    option_labels,
    render_text,
)

from .conftest import tool_call


# ── T1 内容预览构建 ──────────────────────────────────────────────────────────
def test_command_preview_label_and_body():
    prev = build_preview(tool_call("run_command", {"command": "ls -la /tmp"}))
    assert prev.label == "命令"
    assert prev.kind == "command"
    assert prev.lexer == "bash"                       # 命令高亮 lexer = shell/bash
    assert "ls -la /tmp" in "\n".join(ln.text for ln in prev.lines)


def test_edit_preview_diff_del_and_add():
    prev = build_preview(tool_call(
        "edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}
    ))
    assert prev.label == "编辑"
    kinds = {ln.kind for ln in prev.lines}
    assert "del" in kinds and "add" in kinds          # 删行 + 增行
    del_text = "".join(ln.text for ln in prev.lines if ln.kind == "del")
    add_text = "".join(ln.text for ln in prev.lines if ln.kind == "add")
    assert "x = 1" in del_text and "x = 2" in add_text


def test_write_preview_label_path_content():
    prev = build_preview(tool_call(
        "write_file", {"path": "pkg/mod.py", "content": "print('hi')\n"}
    ))
    assert prev.label == "写文件"
    assert prev.header == "pkg/mod.py"                # 路径作框顶标题
    assert prev.lexer == "python"                     # 按扩展名高亮
    assert "print('hi')" in "\n".join(ln.text for ln in prev.lines)
    # 整体渲染同时可见 path 与 content。
    out = render_text(prev, 0, True, width=80)
    assert "pkg/mod.py" in out and "print('hi')" in out


def test_preview_truncation_lines_and_width():
    # 超 20 行 → 截断且尾部 `… +N 行`。
    content = "\n".join(f"line{i}" for i in range(30))
    prev = build_preview(tool_call("write_file", {"path": "a.txt", "content": content}))
    assert len(prev.lines) == 20
    assert prev.extra_lines == 10
    assert "… +10 行" in render_text(prev, 0, True, width=80)
    # 单行超 100 字符 → 该行以 `…` 截断。
    prev2 = build_preview(tool_call("run_command", {"command": "x" * 250}))
    assert prev2.lines[0].text.endswith("…")
    assert len(prev2.lines[0].text) <= MAX_LINE_WIDTH


def test_preview_missing_fields_fallback_no_raise():
    # 空 dict / 缺字段 / 非 dict input：均返回兜底预览、不抛裸异常。
    assert build_preview({}).lines                          # 无 name 无 input
    assert build_preview({"name": "write_file"}).label == "写文件"   # 缺 input
    assert build_preview({"name": "run_command", "input": "oops"}).label == "命令"  # input 非 dict
    assert build_preview({"name": "edit_file", "input": {}}).label == "编辑"        # 缺 old/new


# ── T2 六组件渲染 ────────────────────────────────────────────────────────────
def test_option_labels_exact():
    assert option_labels("edit_file") == [
        "同意执行",
        "同意，本会话内不再询问 edit_file",
        "拒绝",
    ]


def test_render_contains_three_options_exact():
    prev = build_preview(tool_call("write_file", {"path": "a.py", "content": "x"}))
    out = render_text(prev, 0, True, width=80)
    assert "同意执行" in out
    assert "同意，本会话内不再询问 write_file" in out
    assert "拒绝" in out


def test_selected_indicator_and_numbers():
    prev = build_preview(tool_call("run_command", {"command": "ls"}))
    rows = confirm_ui._render_options(prev, 1)
    assert confirm_ui.INDICATOR in rows[1].plain          # 选中项左侧 `›`
    assert rows[0].plain.strip().startswith("1")          # 未选中项显序号
    assert rows[2].plain.strip().startswith("3")
    # 选中项文案用高亮样式，未选中项用次要样式。
    assert rows[1].spans[1].style == confirm_ui.SELECTED_TEXT_STYLE
    assert rows[0].spans[1].style == confirm_ui.UNSELECTED_TEXT_STYLE


def test_thin_rule_between_display_and_action():
    prev = build_preview(tool_call("run_command", {"command": "ls"}))
    out = render_text(prev, 0, True, width=40)
    # 一条「纯 ─」的行即分隔线（Panel 边框含 ╭╮╰╯ 角，不会是纯 ─）。
    assert any(set(line) == {"─"} for line in out.splitlines())


def test_hint_bar_keys_and_low_contrast():
    hint = confirm_ui._render_hint()
    assert "↑↓" in hint.plain and "↵" in hint.plain and "esc" in hint.plain
    assert all("dim" in str(s.style) for s in hint.spans)   # 低对比度样式类


def test_cursor_blink_diff_only_at_cursor():
    prev = build_preview(tool_call("run_command", {"command": "ls"}))
    on = render_text(prev, 0, True, width=60)
    off = render_text(prev, 0, False, width=60)
    assert CURSOR_CHAR in on
    assert CURSOR_CHAR not in off
    # 两次渲染仅差光标字符（替换回空格后应完全一致）。
    assert on.replace(CURSOR_CHAR, " ") == off


def test_edit_render_has_add_and_del_coloring():
    prev = build_preview(tool_call(
        "edit_file", {"path": "a.py", "old_string": "a=1", "new_string": "a=2"}
    ))
    diff = confirm_ui._render_diff(prev)
    styles = [str(s.style) for s in diff.spans]
    assert any("red" in s for s in styles)      # 删行红
    assert any("green" in s for s in styles)    # 增行绿


# ── T3 内联交互应用（pipe input 喂键，DummyOutput 不触屏）─────────────────────
async def _run_keys(keys: str) -> str:
    prev = build_preview(tool_call("run_command", {"command": "ls"}))
    with create_pipe_input() as inp:
        inp.send_text(keys)
        return await asyncio.wait_for(
            confirm_interactive(prev, input=inp, output=DummyOutput()), timeout=5
        )


async def test_enter_returns_approve_on_default():
    assert await _run_keys("\r") == APPROVE                 # 默认选中项 1 → 同意


async def test_down_then_enter_returns_approve_always():
    assert await _run_keys("\x1b[B\r") == APPROVE_ALWAYS    # ↓ 到项 2 → 同意且记住


async def test_two_downs_then_enter_returns_reject():
    assert await _run_keys("\x1b[B\x1b[B\r") == REJECT      # ↓↓ 到项 3 → 拒绝


async def test_down_wraps_around():
    assert await _run_keys("\x1b[B\x1b[B\x1b[B\r") == APPROVE   # ↓×3 回绕到项 1


async def test_up_wraps_around():
    assert await _run_keys("\x1b[A\r") == REJECT            # 从项 1 ↑ 回绕到项 3


async def test_number_keys_direct_select():
    assert await _run_keys("2\r") == APPROVE_ALWAYS         # 数字 2 直选项 2
    assert await _run_keys("3\r") == REJECT                 # 数字 3 直选项 3
    assert await _run_keys("2\x1b[A\r") == APPROVE          # 2 选项2 再 ↑ 回项1


async def test_escape_returns_reject():
    assert await _run_keys("\x1b") == REJECT


async def test_ctrl_c_returns_reject_no_hang():
    assert await _run_keys("\x03") == REJECT                # 不卡死（wait_for 5s 内返回）


def test_blink_interval_is_point_six():
    assert confirm_ui.CURSOR_BLINK_INTERVAL == 0.6


# ── T4 非 TTY 降级 ───────────────────────────────────────────────────────────
async def test_confirm_plain_exact_prompt_and_yes():
    out = io.StringIO()
    res = await confirm_plain("run_command", stdin=io.StringIO("y\n"), stdout=out)
    assert res is True
    assert out.getvalue() == "执行 run_command? [y/N] "    # 提示文案精确


async def test_confirm_plain_non_y_rejects():
    res = await confirm_plain("write_file", stdin=io.StringIO("n\n"), stdout=io.StringIO())
    assert res is False


async def test_confirm_plain_empty_input_defaults_reject():
    res = await confirm_plain("edit_file", stdin=io.StringIO(""), stdout=io.StringIO())
    assert res is False                                     # 空输入（EOF）→ 默认拒绝


def test_is_interactive_false_for_non_tty(monkeypatch):
    monkeypatch.setattr(confirm_ui.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(confirm_ui.sys, "stdout", io.StringIO(""))
    assert confirm_ui.is_interactive() is False
