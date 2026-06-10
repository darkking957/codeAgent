"""富交互执行确认 UI（#0005）。

写类工具执行前的确认环节：把 `[y/N]` 一行问句升级为内联富交互菜单，复刻 Claude Code
CLI 的确认风格——区域标签 / 内容预览块 / 分隔线 / 确认问句 / 选项列表 / 快捷键提示条
六个组件，并把「同意 / 本会话不再询问 / 永久写入规则 / 拒绝」四个出口落到方向键可选菜单
（#0007 在 #0005 三态基础上扩展出 persist 出口）。

分层（便于脱离 TUI 单测）：
  - 预览构建（build_preview）/ 六组件渲染（render_menu 及各 _render_*）都是纯函数：
    输入 = 工具调用 + 选中项 + 光标态，输出 = 结构化预览 / 可渲染内容。
  - 交互应用（confirm_interactive）薄薄一层包在外面：把渲染挂上 prompt_toolkit 非全屏
    内联模式的键绑定与定时刷新，返回三态决策。
  - 非 TTY 降级（confirm_plain）：管道 / 重定向 / CI 下回落纯文本 y/N（默认拒绝）。

四态决策在本模块产出，TUI 侧映射为结构化 ConfirmDecision（once/session/persist/reject）回传
循环（#0007 升级确认契约，supersedes #0004/#0005 的 bool）。
渲染对缺字段 / 异常输入有兜底，最坏回落朴素文本，不拖垮确认。
"""

import asyncio
import difflib
import io
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, Layout, Window
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

# ── 四态决策（confirm_interactive 返回值）；TUI 侧映射为结构化 ConfirmDecision 回传循环 ──
# 在 #0005 三态基础上扩展出 persist 出口（#0007 HITL 三级粒度 once/session/persist + 拒绝）。
APPROVE = "approve"               # 同意执行（once）
APPROVE_ALWAYS = "approve_always"  # 同意，本会话内不再询问此工具（session）
PERSIST = "persist"               # 同意并永久写入规则（persist）
REJECT = "reject"                 # 拒绝 / esc / Ctrl+C
_DECISIONS = [APPROVE, APPROVE_ALWAYS, PERSIST, REJECT]  # 下标 0/1/2/3 对应选项 1/2/3/4

# ── 固定值（见 checklist「固定值」表）──────────────────────────────────────────
# 区域标签文案：按工具名分派。
LABELS = {"run_command": "命令", "write_file": "写文件", "edit_file": "编辑"}
# 当前项指示符（高亮项左侧）。
INDICATOR = "›"
# 光标闪烁周期 / 定时刷新间隔（秒）。
CURSOR_BLINK_INTERVAL = 0.6
# 预览体最大行数；超出截断并显示 `… +N 行`。
MAX_PREVIEW_LINES = 20
# 预览单行最大宽（字符）；超出截断加 `…`。
MAX_LINE_WIDTH = 100
# 非 TTY 降级提示文案（默认拒绝）。
NON_TTY_PROMPT = "执行 {name}? [y/N] "
# 确认问句与问句尾光标字符（cursor_on 控制其有无）。
QUESTION = "是否执行此操作？"
CURSOR_CHAR = "▌"

# ── 配色（并入现有 TUI 主题色板：强调金 #D4A54A / 青 #00D7AF）──────────────────
# 区域标签 pill：强调色底 + 深色字。
LABEL_STYLES = {
    "command": "bold #16160e on #D4A54A",
    "write":   "bold #0e1614 on #00D7AF",
    "edit":    "bold #0e1016 on #5FAFFF",
    "generic": "bold #161616 on #888888",
}
BOX_BORDER_STYLE = "#3a3a3a"
DESC_STYLE = "#777777"        # 框下一行次要色描述
RULE_STYLE = "#2a2a2a"        # 细分隔线（展示区 / 操作区之间）
QUESTION_STYLE = "#D4A54A bold"
SELECTED_MARK_STYLE = "#D4A54A bold"   # 选中项左侧指示符
SELECTED_TEXT_STYLE = "bold #ffffff"   # 选中项文案高亮
NUMBER_STYLE = "#555555"               # 未选中项序号
UNSELECTED_TEXT_STYLE = "#999999"      # 未选中项文案
HINT_STYLE = "dim"                     # 快捷键提示条：低对比度
HINT_KEY_STYLE = "dim reverse"         # 键名：细边框（反显）包裹
DIFF_DEL_STYLE = "red"
DIFF_ADD_STYLE = "green"
DIFF_CTX_STYLE = "dim"

# 写文件按扩展名高亮：扩展名 → pygments lexer 名。
_EXT_LEXER = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".jsx": "javascript",
    ".tsx": "typescript", ".json": "json", ".md": "markdown", ".sh": "bash",
    ".bash": "bash", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".html": "html", ".css": "css", ".go": "go", ".rs": "rust", ".c": "c",
    ".h": "c", ".cpp": "cpp", ".java": "java", ".rb": "ruby", ".sql": "sql",
    ".xml": "xml", ".ini": "ini", ".cfg": "ini",
}


# ── 数据结构 ─────────────────────────────────────────────────────────────────
@dataclass
class PreviewLine:
    """预览体一行：kind 决定着色（add 增 / del 删 / context 上下文 / plain 普通）。"""

    kind: str
    text: str


@dataclass
class Preview:
    """一个工具调用的结构化预览（纯数据，便于单测断言）。"""

    tool_name: str            # 工具名（选项 2 / 非 TTY 文案插值用）
    label: str                # 区域标签文案
    header: str               # 框内顶部路径头（write/edit 显示；命令为空）
    lines: list[PreviewLine] = field(default_factory=list)  # 预览体（已截断）
    description: str = ""      # 框下一行次要色描述
    extra_lines: int = 0       # 行数截断余量（>0 → 渲染 `… +N 行`）
    lexer: str | None = None   # 高亮 lexer（命令=bash；写文件按扩展名；编辑=None 走 diff）
    kind: str = "generic"      # command / write / edit / generic


# ── T1：内容预览构建（纯函数）────────────────────────────────────────────────
def build_preview(tool_call: dict) -> Preview:
    """把一个工具调用映射成结构化预览；缺字段 / 异常输入兜底，不抛裸异常。"""
    try:
        name = str(tool_call.get("name", "?"))
        inp = tool_call.get("input")
        if not isinstance(inp, dict):
            inp = {}
        if name == "run_command":
            return _command_preview(name, inp)
        if name == "write_file":
            return _write_preview(name, inp)
        if name == "edit_file":
            return _edit_preview(name, inp)
        return _generic_preview(name, inp)
    except Exception:  # noqa: BLE001 —— 预览构建绝不拖垮确认，最坏回落兜底文本
        return _fallback_preview(tool_call)


def _command_preview(name: str, inp: dict) -> Preview:
    command = str(inp.get("command", "") or "")
    raw = command.splitlines() or [""]
    lines, extra = _clip([PreviewLine("plain", s) for s in raw])
    return Preview(
        tool_name=name, label=LABELS.get(name, name), header="", lines=lines,
        description="将在 shell 中执行命令", extra_lines=extra, lexer="bash", kind="command",
    )


def _write_preview(name: str, inp: dict) -> Preview:
    path = str(inp.get("path", "") or "")
    content = str(inp.get("content", "") or "")
    raw = content.splitlines() or [""]
    lines, extra = _clip([PreviewLine("plain", s) for s in raw])
    return Preview(
        tool_name=name, label=LABELS.get(name, name), header=path, lines=lines,
        description=f"写入 {path}（{len(content)} 字符）", extra_lines=extra,
        lexer=_lexer_for(path), kind="write",
    )


def _edit_preview(name: str, inp: dict) -> Preview:
    path = str(inp.get("path", "") or "")
    old = str(inp.get("old_string", "") or "")
    new = str(inp.get("new_string", "") or "")
    lines, extra = _clip(_diff(old, new))
    return Preview(
        tool_name=name, label=LABELS.get(name, name), header=path, lines=lines,
        description=f"修改 {path}", extra_lines=extra, lexer=None, kind="edit",
    )


def _generic_preview(name: str, inp: dict) -> Preview:
    """其他（未来）写类工具：以 JSON 转储入参兜底展示。"""
    try:
        import json
        body = json.dumps(inp, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        body = str(inp)
    raw = body.splitlines() or [""]
    lines, extra = _clip([PreviewLine("plain", s) for s in raw])
    return Preview(
        tool_name=name, label=LABELS.get(name, name), header="", lines=lines,
        description=f"执行 {name}", extra_lines=extra, lexer="json", kind="generic",
    )


def _fallback_preview(tool_call) -> Preview:
    """最坏兜底：连工具名都取不出时的朴素预览。"""
    name = "?"
    try:
        name = str(tool_call.get("name", "?"))
    except Exception:  # noqa: BLE001
        pass
    return Preview(
        tool_name=name, label=LABELS.get(name, name), header="",
        lines=[PreviewLine("plain", "（无法构建预览）")], description="", extra_lines=0,
        lexer=None, kind="generic",
    )


def _diff(old: str, new: str) -> list[PreviewLine]:
    """原文→新文行级 diff：删行（del）/ 增行（add）/ 未变（context）。"""
    out: list[PreviewLine] = []
    for d in difflib.ndiff(old.splitlines(), new.splitlines()):
        tag, text = d[:2], d[2:]
        if tag == "- ":
            out.append(PreviewLine("del", text))
        elif tag == "+ ":
            out.append(PreviewLine("add", text))
        elif tag == "  ":
            out.append(PreviewLine("context", text))
        # "? " 是 ndiff 的列内提示行，跳过。
    if not out:
        out.append(PreviewLine("context", ""))
    return out


def _clip(plines: list[PreviewLine]) -> tuple[list[PreviewLine], int]:
    """截断：保留前 MAX_PREVIEW_LINES 行；每行超 MAX_LINE_WIDTH 字符加 `…`。"""
    clipped: list[PreviewLine] = []
    for pl in plines[:MAX_PREVIEW_LINES]:
        text = pl.text
        if len(text) > MAX_LINE_WIDTH:
            text = text[:MAX_LINE_WIDTH - 1] + "…"
        clipped.append(PreviewLine(pl.kind, text))
    extra = max(0, len(plines) - MAX_PREVIEW_LINES)
    return clipped, extra


def _lexer_for(path: str) -> str | None:
    return _EXT_LEXER.get(Path(path).suffix.lower())


# ── T2：六组件渲染（纯函数 → 可渲染内容）─────────────────────────────────────
def option_labels(tool_name: str) -> list[str]:
    """四个选项文案（含工具名插值），见 checklist 固定值。"""
    return [
        "同意执行",
        f"同意，本会话内不再询问 {tool_name}",
        "同意并永久写入规则",
        "拒绝",
    ]


def _render_label(preview: Preview) -> Text:
    """组件①：区域标签 pill（强调色底 + 深色字）。"""
    style = LABEL_STYLES.get(preview.kind, LABEL_STYLES["generic"])
    return Text(f" {preview.label} ", style=style)


def _render_diff(preview: Preview) -> Text:
    """编辑类预览体：删行红 `-`、增行绿 `+`、上下文 dim。"""
    t = Text()
    for i, pl in enumerate(preview.lines):
        if i:
            t.append("\n")
        if pl.kind == "del":
            t.append(f"- {pl.text}", style=DIFF_DEL_STYLE)
        elif pl.kind == "add":
            t.append(f"+ {pl.text}", style=DIFF_ADD_STYLE)
        else:
            t.append(f"  {pl.text}", style=DIFF_CTX_STYLE)
    return t


def _render_body(preview: Preview):
    """预览体：编辑走 diff 着色；其余走 rich Syntax 按 lexer 高亮。附行数截断尾注。"""
    if preview.kind == "edit":
        body = _render_diff(preview)
    else:
        code = "\n".join(pl.text for pl in preview.lines)
        body = Syntax(
            code, preview.lexer or "text", theme="ansi_dark",
            background_color="default", word_wrap=False,
        )
    if preview.extra_lines > 0:
        return Group(body, Text(f"… +{preview.extra_lines} 行", style="dim italic"))
    return body


def _render_box(preview: Preview) -> Panel:
    """组件②：内容预览块（等宽框）。header（路径）作框顶标题。"""
    return Panel(
        _render_body(preview),
        title=preview.header or None,
        title_align="left",
        border_style=BOX_BORDER_STYLE,
        expand=False,
        padding=(0, 1),
    )


def _render_description(preview: Preview) -> Text:
    """预览块下一行次要色描述。"""
    return Text(f"  {preview.description}", style=DESC_STYLE)


def _render_question(cursor_on: bool) -> Text:
    """组件④：确认问句；末尾光标周期性闪烁（cursor_on 控制光标字符有无）。"""
    t = Text()
    t.append(QUESTION, style=QUESTION_STYLE)
    t.append(CURSOR_CHAR if cursor_on else " ", style=QUESTION_STYLE)
    return t


def _render_options(preview: Preview, selected_index: int) -> list[Text]:
    """组件⑤：选项列表。当前项 `›` + 高亮；未选中项显序号。"""
    rows: list[Text] = []
    for i, label in enumerate(option_labels(preview.tool_name)):
        t = Text()
        if i == selected_index:
            t.append(f" {INDICATOR} ", style=SELECTED_MARK_STYLE)
            t.append(label, style=SELECTED_TEXT_STYLE)
        else:
            t.append(f" {i + 1} ", style=NUMBER_STYLE)
            t.append(label, style=UNSELECTED_TEXT_STYLE)
        rows.append(t)
    return rows


def _render_hint() -> Text:
    """组件⑦：快捷键提示条（低对比度；键名反显包裹）。"""
    t = Text()
    t.append(" ↑↓ ", style=HINT_KEY_STYLE)
    t.append(" 移动   ", style=HINT_STYLE)
    t.append(" ↵ ", style=HINT_KEY_STYLE)
    t.append(" 确认   ", style=HINT_STYLE)
    t.append(" esc ", style=HINT_KEY_STYLE)
    t.append(" 取消", style=HINT_STYLE)
    return t


def render_menu(preview: Preview, selected_index: int, cursor_on: bool) -> Group:
    """六组件组装：展示区（标签+预览块+描述）│分隔线│操作区（问句+选项）+ 提示条。"""
    items: list = [_render_label(preview), _render_box(preview)]
    if preview.description:
        items.append(_render_description(preview))
    items.append(Rule(style=RULE_STYLE))           # 组件③：分隔线（展示区/操作区之间）
    items.append(_render_question(cursor_on))
    items.extend(_render_options(preview, selected_index))
    items.append(Text(""))
    items.append(_render_hint())
    return Group(*items)


def _term_width(default: int = 80) -> int:
    try:
        return max(28, shutil.get_terminal_size().columns)
    except Exception:  # noqa: BLE001
        return default


def render_ansi(preview: Preview, selected_index: int, cursor_on: bool, width: int) -> str:
    """渲染成带 ANSI 转义的字符串，供 prompt_toolkit 内联展示。"""
    buf = io.StringIO()
    con = Console(file=buf, width=width, force_terminal=True,
                  color_system="truecolor", highlight=False)
    con.print(render_menu(preview, selected_index, cursor_on), end="")
    return buf.getvalue()


def render_text(preview: Preview, selected_index: int, cursor_on: bool, width: int = 80) -> str:
    """渲染成纯文本（无色），供单测断言文案 / 结构。"""
    buf = io.StringIO()
    con = Console(file=buf, width=width, no_color=True, highlight=False)
    con.print(render_menu(preview, selected_index, cursor_on), end="")
    return buf.getvalue()


# ── T3：内联交互应用（按键 → 三态决策）──────────────────────────────────────
async def confirm_interactive(preview: Preview, *, input=None, output=None) -> str:
    """弹出内联富菜单，返回四态决策（APPROVE / APPROVE_ALWAYS / PERSIST / REJECT）。

    基于 prompt_toolkit 非全屏内联模式：方向键 ↑↓ 导航（回绕）、数字 1·2·3·4 直选、
    回车确认、esc / Ctrl+C 取消（归拒绝）。定时刷新驱动问句尾光标闪烁；确认后不擦除
    （留痕滚动历史）。input/output 仅供测试注入。
    """
    state = {"index": 0, "cursor_on": True}
    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        state["index"] = (state["index"] - 1) % len(_DECISIONS)

    @kb.add("down")
    def _down(event):
        state["index"] = (state["index"] + 1) % len(_DECISIONS)

    # 数字 1..N 直选对应选项（随 _DECISIONS 长度自适应，含 persist 出口）。
    for _i in range(len(_DECISIONS)):
        @kb.add(str(_i + 1))
        def _select(event, _idx=_i):
            state["index"] = _idx

    @kb.add("enter")
    def _enter(event):
        event.app.exit(result=_DECISIONS[state["index"]])

    @kb.add("escape")
    def _esc(event):
        event.app.exit(result=REJECT)

    @kb.add("c-c")
    def _ctrl_c(event):
        event.app.exit(result=REJECT)

    control = FormattedTextControl(
        lambda: ANSI(render_ansi(preview, state["index"], state["cursor_on"], _term_width()))
    )
    window = Window(content=control, dont_extend_height=True, wrap_lines=False)
    app = Application(
        layout=Layout(window),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=False,   # 确认后保留菜单帧在滚动历史（不清屏、不擦除）
        refresh_interval=CURSOR_BLINK_INTERVAL,
        input=input,
        output=output,
    )

    async def _blink():
        while True:
            await asyncio.sleep(CURSOR_BLINK_INTERVAL)
            state["cursor_on"] = not state["cursor_on"]
            app.invalidate()

    blink = asyncio.ensure_future(_blink())
    try:
        result = await app.run_async()
    finally:
        blink.cancel()
    # 兜底：异常退出（如 None）一律按拒绝处理。
    return result if result in _DECISIONS else REJECT


# ── T4：非 TTY 降级（纯文本 y/N）─────────────────────────────────────────────
def is_interactive() -> bool:
    """stdin 与 stdout 同为 TTY 才走富交互；否则（管道 / 重定向 / CI）降级。"""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


async def confirm_plain(name: str, *, stdin=None, stdout=None) -> bool:
    """非 TTY 纯文本确认：写出 `执行 <name>? [y/N] `，默认拒绝；y/yes 才同意。"""
    fin = stdin if stdin is not None else sys.stdin
    fout = stdout if stdout is not None else sys.stdout
    prompt = NON_TTY_PROMPT.format(name=name)

    def _ask() -> str:
        try:
            fout.write(prompt)
            fout.flush()
            return fin.readline()
        except (OSError, ValueError):
            return ""

    try:
        ans = await asyncio.to_thread(_ask)
    except Exception:  # noqa: BLE001 —— 读取失败按拒绝
        return False
    return ans.strip().lower() in ("y", "yes")
