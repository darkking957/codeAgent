import asyncio
import contextlib
import json
import signal
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from coreagent import confirm_ui
from coreagent.agent import (
    ConfirmDecision,
    run_agent_turn,
)
from coreagent.commands import (
    CommandResult,
    build_command_registry,
    parse,
    unknown_command_message,
)
from coreagent.config import is_max_enabled
from coreagent.events import (
    Retry,
    RunDone,
    RunEndReason,
    RunError,
    TextDelta,
    TextKind,
    ToolCall,
    ToolResultEvent,
    TurnStart,
)
from coreagent.hooks.models import (
    SESSION_END,
    SESSION_START,
    STOP,
    USER_PROMPT_SUBMIT,
)
from coreagent.permissions import modes
from coreagent.prompts import build_system_prompt
from coreagent.skills.commands import (
    shadowed_skills,
    shared_trigger_prompt,
    skill_completions,
)
from coreagent.skills.independent import run_independent_skill
from coreagent.skills.types import SkillMode
from coreagent.tools import build_registry

# ── ANSI codes for token-by-token streaming ────────────────────────────────────
_AI      = "\033[96m"      # bright cyan  – ◆ model header
_TH_HDR  = "\033[2;36m"   # dim cyan     – ◈ thinking label
_TH_BODY = "\033[2m"      # dim          – thinking body
_SP      = "\033[2;36m"   # dim cyan     – spinner
_RST     = "\033[0m"

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# 状态栏档位图标（#0017 T7）：default 不展示；其余按档区分。文本仍带档名（含 acceptEdits / auto）。
_MODE_ICONS = {
    modes.PLAN: "⏸",
    modes.ACCEPT_EDITS: "✎",
    modes.AUTO: "⚡",
    modes.BYPASS: "⚙",
}

# /mode auto 被 Max 门控拒绝时的精确提示（#0017 T4，见 checklist 钉死值）。
MODE_AUTO_NEEDS_MAX = "auto 档需要 Max 资格（当前未开启）"


def format_model_header(model: str, stat: str | None = None) -> str:
    """模型抬头行文本（#0009 可单测助手）：`◆ {model}`，有统计则在行内追加 `Xk / Yk`。"""
    line = f" {_AI}◆  {model}{_RST}"
    if stat:
        line += f"  {_TH_BODY}{stat}{_RST}"
    return line

# ── cora mascot art – minimal fox ─────────────────────────────────────────────

_CORA_GREET = """\
  /\\  /\\
 ( ^.^ )
  \\___/
   cora"""

_CORA_BYE = """\
  /\\  /\\
 ( ^.^ )/
  \\___/
   cora"""

_CORA_REGEX = (
    r"(?P<ears>[/\\])"
    r"|(?P<eyes>\^)"
    r"|(?P<nose>\.)"
    r"|(?P<name>cora)"
)

_CORA_THEME = Theme({
    "cora.ears": "#D4A54A bold",
    "cora.eyes": "bold bright_cyan",
    "cora.nose": "#D4A54A",
    "cora.name": "dim italic",
})


def _cora(state: str = "greet") -> Text:
    """Return a styled Rich Text of the cora fox mascot."""
    art = _CORA_BYE if state == "bye" else _CORA_GREET
    t = Text(art, style="")
    t.highlight_regex(_CORA_REGEX, style_prefix="cora.")
    return t


# ── prompt_toolkit global style ────────────────────────────────────────────────
_STYLE = Style.from_dict({
    "prompt":                                "#D4A54A bold",
    "bottom-toolbar":                        "bg:#0f0f0f noreverse",
    "bottom-toolbar.brand":                  "fg:#00D7AF bold bg:#0f0f0f",
    "bottom-toolbar.sep":                    "fg:#2a2a2a bg:#0f0f0f",
    "bottom-toolbar.info":                   "fg:#555555 bg:#0f0f0f",
    "bottom-toolbar.hint":                   "fg:#333333 bg:#0f0f0f",
    "completion-menu.completion":            "bg:#111c2a fg:#7aadcc",
    "completion-menu.completion.current":    "bg:#00aacc fg:#ffffff bold",
    "completion-menu.meta.completion":       "bg:#0c1520 fg:#445566",
    "completion-menu.meta.completion.current": "bg:#007a99 fg:#ffffff",
    "scrollbar.background":                  "bg:#0f0f0f",
    "scrollbar.button":                      "bg:#333333",
    "auto-suggest":                          "fg:#2e2e2e",
})

_HISTORY_PATH = Path("~/.config/coreagent/prompt_history").expanduser()

# 主输入框提示符。注意：prompt_toolkit 的 `prompt_async(message=...)` 会执行
# `self.message = message`，永久改写共享 session 的提示符——confirm / plan 复用同一
# session 传了自己的 message 后会污染主输入框。故主循环每次显式传回本常量复位。
_PROMPT_MSG = FormattedText([("class:prompt", " ◇  ")])

# 团队功能（#0016 T13）：Lead 空闲等输入时被团队消息唤醒的哨兵 + 唤醒后排的 drain turn 合成提示。
_WAKE = object()
_LEAD_DRAIN_PROMPT = "（团队成员发来了新消息，请查看 team-inbox 并决定下一步：回复 / 派活 / 汇总）"


# ── /command completer（命令注册中心 + 技能次级来源，元数据单一来源）──────────────
class _CommandCompleter(Completer):
    def __init__(self, commands, skills=None) -> None:
        # 注册中心：补全候选（排除隐藏命令与别名条目）即时取自此处。
        self._commands = commands
        # 技能仓（#0012）：技能名作为补全的次级来源（排除被内置命令遮蔽的名字）。
        self._skills = skills

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return
        candidates = list(self._commands.completions(text))
        if self._skills is not None:
            taken = {name.lstrip("/") for name, _ in candidates}
            candidates += skill_completions(self._skills, text, taken)
        for name, summary in candidates:
            yield Completion(
                name[len(text):],
                display=name,
                display_meta=summary,
                style="fg:cyan",
                selected_style="fg:white bg:cyan bold",
            )


# ── waiting spinner ────────────────────────────────────────────────────────────
async def _run_spinner(stop: asyncio.Event) -> None:
    i = 0
    while not stop.is_set():
        frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
        sys.stdout.write(f"\r {_SP}{frame}  waiting…{_RST}   ")
        sys.stdout.flush()
        await asyncio.sleep(0.08)
        i += 1
    sys.stdout.write(f"\r{' ' * 22}\r")
    sys.stdout.flush()


# ── TUI ────────────────────────────────────────────────────────────────────────
class TUI:
    def __init__(self, provider, config, conversation, registry=None, system_prompt=None,
                 env_block=None, pipeline=None, mcp_manager=None, context=None,
                 instructions_block=None, session_memory=None, notes_store=None,
                 commands=None, skills=None, hooks=None, background_manager=None,
                 worktree_manager=None, team_manager=None):
        self.provider = provider
        self.config = config
        self.conversation = conversation
        # 项目根（#0015）：主 Agent 的 cwd——不 chdir，按调用透传给 run_agent_turn，工具据此解析相对路径。
        self._project_root = str(Path.cwd())
        # Git Worktree 隔离子系统（#0015）：由 main 注入；run() 启动时挂周期清理任务、回收过期工作树。
        # 缺省 None 时无隔离（清理任务不挂载）。
        self.worktree_manager = worktree_manager
        # 斜杠命令注册中心（#0011）：由 main 注入（启动期已做冲突检测）；缺省时自建，便于测试构造。
        # 补全器 / /help / 主循环分流三处都从此单一来源读，不再各存命令字典。
        self.commands = commands if commands is not None else build_command_registry()
        # 技能仓（#0012）：由 main 注入；缺省 None 时无技能系统（旧测试构造不受影响）。
        # 技能名作命令分发 / 补全的次级来源；目录块作 system 尾部块注入；激活集每轮重算工具列表。
        self.skills = skills
        self._skills_catalog_block: str | None = None
        if self.skills is not None:
            self._skills_catalog_block = self.skills.catalog_block()
            # 遮蔽检测：技能名与内置命令（名 / 别名）冲突 → 命令优先、技能被遮蔽，记 warning（不致命）。
            taken = {c.name for c in self.commands.visible()}
            taken |= {a for c in self.commands.visible() for a in c.aliases}
            for shadowed in shadowed_skills(self.skills, taken):
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "技能「%s」与内置命令同名，已被遮蔽（命令优先）", shadowed
                )
        # 记忆系统（#0010）：由 main 注入；缺省 None 时无记忆（旧测试构造不受影响）。
        # instructions_block 作 system 尾部块每轮注入；session_memory / notes_store 驱动恢复与钩子。
        self.instructions_block = instructions_block
        self.session_memory = session_memory
        self.notes_store = notes_store
        # 启动恢复 + 长期记忆索引组装出的注入文本（仅首轮注入一次）；run() 内异步初始化后回填。
        self._memory_block: str | None = None
        # 后台记忆钩子任务（笔记 + 摘要）：与下一轮串行、防竞态；进程退出前等其落定。
        self._memory_task: asyncio.Task | None = None
        # 授权流水线（#0007）：由 main 装配注入；缺省 None 时门禁退回既有默认（读放行/写确认）。
        self.pipeline = pipeline
        # 生命周期 Hook 管理器（#0013）：由 main 装配注入；单一实例贯穿会话，透传给 run_agent_turn
        # 与 context manager。缺省 None 时所有挂点 no-op（无 hooks.yaml / 加载失败时退化为无 Hook）。
        self.hooks = hooks
        # 子 Agent 后台管理器（#0014）：由 main 注入；run() 启动时 bind_loop（同步桥投递子 Agent
        # 协程到主循环用），主循环每轮装配 system 前 drain 后台结果后缀。缺省 None 时无委派系统。
        self.background_manager = background_manager
        # 团队编排枢纽（#0016）：由 main 在实验开关开启时注入；缺省 None 时整套团队能力不存在
        # （行为与现状逐字节一致）。team_manager 非空 ⟺ 当前会话是 Team Lead。
        self.team_manager = team_manager
        # delegate（协调）模式（#0016 T12）：正交布尔状态（不进权限阶梯 modes._RANK）；仅团队功能
        # 开启且为 Lead 时可经 Shift+Tab 切换。开启时把 Lead 工具集收窄到读类 + shell + 团队工具。
        self.delegate = False
        # MCP 管理器（#0008）：由 main 注入；退出路径负责优雅关闭、回收子进程。缺省 None 时无 MCP。
        self.mcp_manager = mcp_manager
        # 上下文管理器（#0009）：由 main 注入；透传给 run_agent_turn，并在模型抬头行展示实时统计。
        # 缺省 None 时全程无上下文管理（统计不显示、不压缩），旧测试构造不受影响。
        self.context = context
        # 把 Hook 管理器接到 context manager（PreCompact/PostCompact 挂点）：单一实例贯穿，
        # 无论 context 由 main 如何构造都保持一致。
        if self.hooks is not None and self.context is not None:
            self.context.hooks = self.hooks
        # 工具注册中心与系统提示词由 main 注入；缺省时自建，便于测试构造。
        self.registry = registry if registry is not None else build_registry()
        # 给 run_command 注入实时回显 sink：命令运行期间逐行输出 / 心跳 / 收尾打到终端，
        # 解决「执行无反馈」。工具本身 UI 无关，writer 由本 UI 层装配（无该工具时静默跳过）。
        _rc = self.registry.get_optional("run_command")
        if _rc is not None and hasattr(_rc, "set_writer"):
            _rc.set_writer(self._tool_output_writer)
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else build_system_prompt(self.registry.names())
        )
        # 环境块（带 <env> 标签）由 main 启动时快照一次注入；缺省 None 时不注入。
        self.env_block = env_block
        self._in_thinking = False
        # 段落空行刷出标记（#0018）：某轮模型流式开始（TurnStart）置位；该轮首个非文本事件
        # （工具调用 / 终止 / 错误）到达时刷出一次「文本块后空行」并复位——与 DONE 事件粒度解耦。
        self._round_streaming = False
        # 权限模式（#0007，把 #0004 的 plan-only 布尔升级为五级模式枚举）：初值取作用域
        # defaultMode（无 pipeline 时为 default）。plan 模式等同旧 plan-only（写类拦截记为计划项）。
        _initial_mode = pipeline.default_mode if pipeline is not None else modes.DEFAULT_MODE
        # auto 默认档受 Max 门控（#0017）：解析到 auto 而 Max 资格未开 → 回落 default。
        if modes.normalize_mode(_initial_mode) == modes.AUTO and not self._max_eligible():
            _initial_mode = modes.DEFAULT_MODE
        self.mode = _initial_mode
        # 本会话信任集（#0005）：选「本会话允许 / 永久写入」后按工具名记住，仅本会话内存；
        # 之后该工具的写操作直接放行、不再弹菜单（persist 另落盘，见 _confirm）。
        self._trusted: set[str] = set()
        self.console = Console(
            markup=True,
            highlight=False,
            theme=_CORA_THEME,
        )

        # ── key bindings ───────────────────────────────────────────────────────
        kb = KeyBindings()

        @kb.add("c-l")
        def _cls(event):
            event.app.output.write_raw("\033[2J\033[H")
            event.app.output.flush()

        # Shift+Tab（#0017 T5，supersedes #0016 键位）：在权限模式间正向循环
        # （default → acceptEdits → plan，Max 资格开追加 auto；bypass 不入循环、按此键回起点）。
        # delegate 切换移到 /delegate 命令，不再挂此键。
        @kb.add("s-tab")
        def _cycle_mode(event):
            self.set_mode(modes.next_mode(self.mode, self._max_eligible()))
            event.app.invalidate()  # 立即重绘状态栏，反映新档

        # ── persistent prompt history ──────────────────────────────────────────
        try:
            _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(_HISTORY_PATH))
        except OSError:
            history = InMemoryHistory()

        # ── bottom toolbar ─────────────────────────────────────────────────────
        def _toolbar():
            parts: list[tuple[str, str]] = [
                ("class:bottom-toolbar.brand", " ◆ CoreAgent"),
                ("class:bottom-toolbar.sep",   "  │"),
                ("class:bottom-toolbar.info",  f"  {config.protocol}"),
                ("class:bottom-toolbar.sep",   " · "),
                ("class:bottom-toolbar.info",  config.model),
            ]
            if config.thinking.enabled:
                parts += [
                    ("class:bottom-toolbar.sep",  "  │"),
                    ("class:bottom-toolbar.info", "  ◈"),
                ]
            if self.mode != modes.DEFAULT:
                # 档名直接展示（含 acceptEdits / auto）；图标按档区分（#0017 T7）。
                icon = _MODE_ICONS.get(self.mode, "⚙")
                parts += [
                    ("class:bottom-toolbar.sep",   "  │"),
                    ("class:bottom-toolbar.brand", f"  {icon} {self.mode}"),
                ]
            # delegate 标记（#0016 T12）：在权限档之外叠加显示；切换即时反映（状态栏每次渲染读 self.delegate）。
            if self.delegate:
                parts += [
                    ("class:bottom-toolbar.sep",   "  │"),
                    ("class:bottom-toolbar.brand", "  ⇄ delegate"),
                ]
            parts += [
                ("class:bottom-toolbar.sep",  "  │"),
                ("class:bottom-toolbar.hint", "  Tab  ↑↓  /help "),
            ]
            return FormattedText(parts)

        self.session = PromptSession(
            message=_PROMPT_MSG,
            style=_STYLE,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=_CommandCompleter(self.commands, self.skills),
            complete_while_typing=True,
            bottom_toolbar=_toolbar,
            key_bindings=kb,
        )

    # ── banner ─────────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        # left: cora mascot
        mascot_col = _cora("greet")

        # right: project info
        info_col = Text()
        info_col.append("◆  CoreAgent\n\n", style="bold bright_cyan")
        info_col.append("  provider   ", style="dim")
        info_col.append(self.config.protocol + "\n", style="bright_cyan")
        info_col.append("  model      ", style="dim")
        info_col.append(self.config.model + "\n", style="bright_cyan")
        info_col.append("  thinking   ", style="dim")
        info_col.append(
            "on\n" if self.config.thinking.enabled else "off\n",
            style="bright_green" if self.config.thinking.enabled else "dim",
        )
        info_col.append("\n  ")
        info_col.append("/help", style="dim cyan")
        info_col.append("  to get started", style="dim")

        layout = Table.grid(padding=(0, 4))
        layout.add_column(vertical="middle")
        layout.add_column(vertical="middle")
        layout.add_row(mascot_col, info_col)

        self.console.print()
        self.console.print(
            Panel(layout, border_style="cyan", padding=(1, 2), expand=False)
        )
        self.console.print()

    # ── goodbye ──────────────────────────────────────────────────────────────────

    def _print_goodbye(self) -> None:
        """/exit、/quit 退出时的吉祥物收尾。"""
        self.console.print()
        self.console.print(_cora("bye"))
        self.console.print("\n  [dim]goodbye[/dim]\n")

    # ── retry notice ─────────────────────────────────────────────────────────────

    def _on_retry(self, attempt: int, wait: int) -> None:
        sys.stdout.write(f"\n第 {attempt} 次重试（{wait}s 后）…\n")
        sys.stdout.flush()

    def _rollback_pending_user(self) -> None:
        """流式失败 / 中断时回滚末条「纯文本 user 提问」，不污染历史。

        仅当末条是字符串内容的 user 消息（即本回合尚未进入工具阶段）才回滚；
        若已是 tool_result（list 内容）则保留，避免留下悬挂的 tool_use 块。
        """
        msgs = self.conversation.messages
        if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], str):
            msgs.pop()

    # ── 事件 → 渲染映射（#0018）───────────────────────────────────────────────────

    def _flush_text_block_if_pending(self) -> None:
        """模型文本块结束 → 刷出段落空行（+收尾 thinking 暗色）；幂等，每轮至多一次。

        替代旧 DONE 事件驱动的 `\\n\\n`：本轮流式开始置 `_round_streaming`，首个非文本事件
        （工具调用 / 终止 / 错误 / 下一轮）到达时由此刷出，与 usage / 事件粒度解耦。
        """
        if not self._round_streaming:
            return
        if self._in_thinking:
            sys.stdout.write(_RST)
            self._in_thinking = False
        sys.stdout.write("\n\n")
        sys.stdout.flush()
        self._round_streaming = False

    def _render_event(self, ev) -> None:
        """渲染一个引擎事件；纯对话 / 多轮 / 工具 / 重试 / 取消 / 上限 / 错误外在行为与重构前一致。"""
        if isinstance(ev, TextDelta):
            if ev.kind is TextKind.THINKING:
                if not self._in_thinking:
                    sys.stdout.write(f" {_TH_HDR}◈  thinking{_RST}\n{_TH_BODY}")
                    self._in_thinking = True
                sys.stdout.write(ev.text)
            else:  # answer
                if self._in_thinking:
                    sys.stdout.write(f"{_RST}\n")
                    self._in_thinking = False
                sys.stdout.write(ev.text)
            sys.stdout.flush()
            return
        if isinstance(ev, Retry):
            self._on_retry(ev.attempt, ev.wait)
            return
        if isinstance(ev, TurnStart):
            # 进入新一轮：兜底刷出上一轮待刷段落（一般其首个非文本事件已刷过），再标记本轮流式开始。
            self._flush_text_block_if_pending()
            # 第 2 轮起显示分组分隔行；单轮纯对话不显示（与 #0003 视觉一致）。
            if ev.round_index >= 2:
                self.console.print(f"[dim]─── 第 {ev.round_index} 轮 ───[/dim]")
            self._round_streaming = True
            return
        # 其余非文本事件：先刷出模型文本块后的段落空行，再渲染本事件。
        self._flush_text_block_if_pending()
        if isinstance(ev, ToolCall):
            self._render_tool("call", ev.call)
        elif isinstance(ev, ToolResultEvent):
            if ev.rejected:
                self._render_tool("rejected", ev.call)
            else:
                self._render_tool("result", ev.call, ev.result)
        elif isinstance(ev, RunDone):
            if ev.reason is RunEndReason.MAX_TURNS:
                self.console.print("  [yellow]●[/yellow]  [dim]已达上限，停止继续调用[/dim]")
            elif ev.reason is RunEndReason.CANCELLED:
                self.console.print("  [dim]已取消[/dim]")
            # RunEndReason.COMPLETE：不额外渲染。
        elif isinstance(ev, RunError):
            self.console.print(f"  [red]✗[/red]  [dim]循环错误：{ev.error_type}[/dim]")

    # ── tool call / result render ────────────────────────────────────────────────

    def _render_tool(self, phase: str, tool_call: dict, result=None) -> None:
        """以紧凑形式展示工具调用与结果。"""
        name = tool_call.get("name", "?")
        if phase == "call":
            try:
                args = json.dumps(tool_call.get("input", {}), ensure_ascii=False)
            except (TypeError, ValueError):
                args = str(tool_call.get("input", {}))
            if len(args) > 120:
                args = args[:117] + "…"
            self.console.print(f"  [cyan]⚙[/cyan]  [bold]{name}[/bold] [dim]{args}[/dim]")
        elif phase == "rejected":
            self.console.print(f"  [red]✗[/red]  [dim]已拒绝执行 {name}[/dim]")
        elif phase == "result" and result is not None:
            mark = "[green]✓[/green]" if result.success else "[red]✗[/red]"
            first = result.content.splitlines()[0] if result.content else ""
            if len(first) > 100:
                first = first[:97] + "…"
            extra = ""
            line_count = result.content.count("\n") + 1 if result.content else 0
            if line_count > 1:
                extra = f" [dim](+{line_count - 1} 行)[/dim]"
            self.console.print(f"  {mark}  [dim]{first}[/dim]{extra}")

    def _tool_output_writer(self, line: str) -> None:
        """run_command 实时回显 sink：命令运行期间的每行输出 / 心跳 / 收尾以缩进灰字打到终端。

        从 run_command 的 reader 线程调用（registry.execute 经 to_thread 跑工具）；用裸 stdout
        写、不走 Rich——避免跨线程渲染竞争。此刻事件循环正 await 工具、主线程无并发输出，安全。
        """
        sys.stdout.write(f"     {_TH_BODY}{line}{_RST}\n")
        sys.stdout.flush()

    # ── execute-time confirmation ────────────────────────────────────────────────

    # ── 授权门禁（#0007）──────────────────────────────────────────────────────────

    def _gate(self, tool_name: str, tool_input: dict):
        """工具执行前的统一前置门禁：用当前模式跑流水线得到 Decision。

        读取 self.mode（每次调用即时取，故 /mode 切换即时生效）；无 pipeline 时
        run_agent_turn 不会调用本方法（gate 传 None），退回既有默认。
        """
        return self.pipeline.decide(tool_name, tool_input, self.mode)

    def mode_command(self, args: str) -> None:
        """/mode 视图 / 切换（实现 UIControl.mode_command；保留既有高级档切换路径）。

        无参数 → 显示当前模式 + 合法集合；带参数 → 切到指定模式（非法名提示合法集合）。
        """
        target = args.strip()
        # 可选档集随 Max 资格即时计算（#0017）：Max 关时隐去 auto，且 /mode auto 被拒。
        available = modes.available_modes(self._max_eligible())
        if not target:
            self.console.print(
                f"  [dim]当前模式：[/dim][bold]{self.mode}[/bold]"
                f"  [dim]可选：{' / '.join(available)}[/dim]"
            )
            return
        if target == modes.AUTO and not self._max_eligible():
            self.console.print(f"  [red]✗[/red]  [dim]{MODE_AUTO_NEEDS_MAX}[/dim]")
            return
        if target not in available:
            self.console.print(
                f"  [red]✗[/red]  [dim]未知模式：{target}；可选：{' / '.join(available)}[/dim]"
            )
            return
        self.set_mode(target)
        note = ""
        if target == modes.BYPASS:
            note = "（跳过权限层，仅限隔离容器）"
        elif target == modes.ACCEPT_EDITS:
            note = "（文件编辑自动批准）"
        elif target == modes.AUTO:
            note = "（安全操作自动放行、高风险转人工确认）"
        elif target == modes.PLAN:
            note = "（只规划、不执行写操作）"
        self.console.print(f"  [dim]已切换权限模式 → [/dim][bold]{target}[/bold] [dim]{note}[/dim]")

    async def _confirm(self, tool_call: dict) -> ConfirmDecision:
        """写类工具命中 ask 时的用户确认（#0005 富菜单 + #0007 四态契约）。

        返回结构化 ConfirmDecision（once/session/persist/reject），承载 HITL 三级粒度：
          - 非 TTY（管道/重定向/CI）→ 纯文本 y/N 降级（同意=once、否则 reject；不记忆、不落盘）；
          - 工具名已在本会话信任集 → 直接 once（不弹菜单，复用 #0005）；
          - 否则弹内联富菜单四态：同意(once) / 本会话允许(session) / 永久写入规则(persist) / 拒绝。
            session、persist 都把工具名加入信任集（本会话不再弹）；persist 另把一条 allow 规则
            落盘到 Local 作用域文件（下次新会话直接放行）。

        不复用主 session（避免再污染 self.message）；富菜单渲染 / 交互全在 confirm_ui 内收敛。
        """
        name = tool_call.get("name", "?")
        # 非 TTY（管道 / 重定向 / CI）→ 纯文本降级，不渲染富 UI、不记忆、不落盘。
        if not confirm_ui.is_interactive():
            ok = await confirm_ui.confirm_plain(name)
            return ConfirmDecision.ONCE if ok else ConfirmDecision.REJECT
        # 本会话已信任该工具 → 直接放行。
        if name in self._trusted:
            return ConfirmDecision.ONCE
        preview = confirm_ui.build_preview(tool_call)
        try:
            decision = await confirm_ui.confirm_interactive(preview)
        except (EOFError, KeyboardInterrupt):
            return ConfirmDecision.REJECT
        if decision == confirm_ui.APPROVE_ALWAYS:
            self._trusted.add(name)   # 本会话记住（内存、不落盘）
            return ConfirmDecision.SESSION
        if decision == confirm_ui.PERSIST:
            self._trusted.add(name)   # 本会话也不再弹
            self._persist_rule(tool_call)
            return ConfirmDecision.PERSIST
        if decision == confirm_ui.APPROVE:
            return ConfirmDecision.ONCE
        return ConfirmDecision.REJECT

    def _persist_rule(self, tool_call: dict) -> None:
        """把一次调用落盘为一条 Local allow 规则（无 pipeline 时降级为仅本会话信任）。"""
        if self.pipeline is None:
            return
        try:
            rule = self.pipeline.persist_allow(
                tool_call.get("name", "?"), tool_call.get("input") or {}
            )
            self.console.print(
                f"  [dim]✓ 已写入规则到 {self.pipeline.local_path}：{rule}[/dim]"
            )
        except Exception as e:  # noqa: BLE001 —— 落盘失败不拖垮确认，降级为本会话信任
            self.console.print(f"  [red]✗[/red]  [dim]写入规则失败：{e}[/dim]")

    async def _review_plan(self, plan: list) -> None:
        """plan-only 循环结束后展示计划列表并征求确认；确认即退出 plan-only（不自动执行）。"""
        self.console.print()
        self.console.print("  [bold]待执行计划：[/bold]")
        for i, item in enumerate(plan, 1):
            self.console.print(f"    [cyan]{i}.[/cyan] [dim]{item.get('text', item.get('name', '?'))}[/dim]")
        try:
            ans = await self.session.prompt_async(
                message=FormattedText([
                    ("class:prompt", "  确认计划并退出 plan-only？[y/N] "),
                ]),
            )
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans.strip().lower() in ("y", "yes"):
            self.mode = modes.DEFAULT
            self.console.print("  [dim]已退出 plan 模式（切回 default）[/dim]")
        else:
            self.console.print("  [dim]仍在 plan 模式；下一条输入仍只规划[/dim]")

    # ── UIControl 接口实现（#0011）：命令逻辑只经本组方法取能力 / 领域数据，与渲染框架解耦。──

    def show(self, text: str) -> None:
        """打印一条命令消息（以纯文本渲染，避免命令文案里的方括号被当富文本标记）。"""
        self.console.print(Text(text), style="dim")

    def list_commands(self) -> list:
        """非隐藏命令列表（供 /help 由注册中心数据生成）。"""
        return self.commands.visible()

    def get_mode(self) -> str:
        return self.mode

    def set_mode(self, mode: str) -> None:
        """切换权限模式；状态栏每次渲染即时读 self.mode（_toolbar），故下次提示即反映。"""
        self.mode = mode

    def _max_eligible(self) -> bool:
        """Max 资格（#0017，占位）：经 config 只读访问器读取，将来替换真实校验不改调用点。"""
        return is_max_enabled(self.config)

    def model_info(self) -> dict:
        """当前模型信息（供 /model 无参查看）。"""
        return {"model": self.config.model, "protocol": self.config.protocol}

    def set_model(self, name: str) -> None:
        """切换本会话使用的模型（运行时即时生效）。

        直接改 ``config.model``：provider / context / 状态栏同持该 config 引用，且模型在每次请求时
        按 ``model_override or config.model`` 即时读取（见 providers.*），故下一回合即用新模型。
        """
        self.config.model = name

    # ── delegate（协调）模式（#0016 T12；#0017 T6：触发入口从 Shift+Tab 移到 /delegate）──────
    def is_lead(self) -> bool:
        """当前会话是否为 Team Lead（团队功能开启 ⟺ 主会话即 Lead）。"""
        return self.team_manager is not None

    def toggle_delegate(self) -> bool | None:
        """切换 delegate 开/关；仅团队功能开启且为 Lead 时生效。

        返回切换后的新状态（True=已开启 / False=已关闭）；团队功能未开启（非 Lead）→ None
        （不接管，delegate 保持 False）。供 /delegate 命令据此给反馈（#0017 T6）。
        """
        if not self.is_lead():
            return None
        self.delegate = not self.delegate
        return self.delegate

    def _delegate_filter(self) -> set[str] | None:
        """delegate 开启时的工具收窄集（读类 + shell + 团队工具）；未开 / 非 Lead → None（不收窄）。"""
        if not self.delegate or not self.is_lead():
            return None
        from coreagent.teams.tools import compute_delegate_filter

        return compute_delegate_filter(self.registry)

    # ── 团队邮箱（#0016 T13）─────────────────────────────────────────────────────────
    def _drain_lead_inbox(self) -> str | None:
        """drain Lead 邮箱未读消息为 team-inbox 注入块（消费即清）；无团队 / 无消息 → None。"""
        tm = self.team_manager
        if tm is None or tm.mailbox is None:
            return None
        from coreagent.teams.mailbox import build_inbox_block

        return build_inbox_block(tm.mailbox.drain(tm.lead_name))

    async def _read_user_input(self):
        """读用户输入；团队功能开（Lead）且空闲时，新团队消息可中断输入等待、排一个 drain turn。

        返回输入字符串，或 ``_WAKE`` 哨兵（被消息唤醒）。EOF / KeyboardInterrupt 照常上抛由调用方处理。
        团队功能关闭时等价于裸 ``prompt_async``（行为与现状一致，无 racing 开销）。**绝不**打断运行中的
        turn——唤醒只在 Lead 空闲等输入（本方法）时发生，运行中的 turn 走另一条路径、不经此。
        """
        if self.team_manager is None:
            return await self.session.prompt_async(message=_PROMPT_MSG)
        coord = self.team_manager.coordinator
        lead = self.team_manager.lead_name
        coord.mark_idle(lead)
        prompt_task = asyncio.ensure_future(self.session.prompt_async(message=_PROMPT_MSG))
        wake_task = asyncio.ensure_future(coord.wait_wake(lead))
        try:
            done, _pending = await asyncio.wait(
                {prompt_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            coord.mark_busy(lead)
        if prompt_task in done:
            wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wake_task
            return prompt_task.result()     # 可能抛 EOFError / KeyboardInterrupt → 上抛
        # 被团队消息唤醒：取消输入等待、排一个 drain turn。
        prompt_task.cancel()
        with contextlib.suppress(BaseException):
            await prompt_task
        return _WAKE

    def token_stats(self) -> str | None:
        if self.context is None:
            return None
        return self.context.format_stats(self.conversation.get_messages())

    def has_context(self) -> bool:
        return self.context is not None

    def _render_background_status(self) -> None:
        """一行后台子 Agent 简报（#0014 T12）：运行中数量 + 各任务状态/用量。无任务则不打印。"""
        if self.background_manager is None:
            return
        tasks = self.background_manager.tasks()
        if not tasks:
            return
        running = self.background_manager.running_count()
        parts = []
        for t in tasks:
            usage = f"，{t.usage_total // 1000}k tok" if t.usage_total else ""
            parts.append(f"{t.agent_id}={t.status}{usage}")
        self.console.print(
            f"  [dim]后台子 Agent（{running} 运行中）：{' / '.join(parts)}[/dim]"
        )

    def detach_foreground_agent(self) -> None:
        """把当前运行中的前台子 Agent 手动切到后台（#0014 T12，spec 能力 13 ③）。

        前台委派阻塞期间，调用此方法置 detach 信号 → AgentTool 的前台等待轮询到即 detach、
        把子 Agent 留在后台继续跑。同一时刻至多一个前台子 Agent 在等，故单信号足够。
        """
        if self.background_manager is not None:
            self.background_manager.request_detach()

    async def compact(self) -> str:
        """以窄余量手动触发一次上下文压缩（先卸载、再按手动阈值判摘要）；返回 before → after 统计。"""
        before = self.context.format_stats(self.conversation.get_messages())
        await self.context.before_request(self.conversation, manual=True)
        self.conversation.save()
        after = self.context.format_stats(self.conversation.get_messages())
        return f"{before} → {after}"

    def clear_history(self) -> None:
        self.conversation.clear()
        self.conversation.save()
        # 清空联动（#0012）：清除已激活技能、注销动态注册的专属工具、恢复全量工具列表。
        if self.skills is not None:
            self.skills.clear()

    def memory_index(self) -> str | None:
        if self.notes_store is None:
            return None
        return self.notes_store.render_index() or None

    def session_info(self) -> dict:
        return {
            "count": len(self.conversation.get_messages()),
            "session_id": self.session_memory.session_id if self.session_memory else None,
        }

    def permission_info(self) -> dict:
        if self.pipeline is None:
            return {"mode": self.mode, "deny": 0, "ask": 0, "allow": 0, "local_path": None}
        m = self.pipeline.merged_rules
        return {
            "mode": self.mode,
            "deny": len(m.deny),
            "ask": len(m.ask),
            "allow": len(m.allow),
            "local_path": str(self.pipeline.local_path),
        }

    def skills_overview(self) -> list[dict] | None:
        """已发现技能概览（供 /skill list）；未启用技能系统为 None。"""
        if self.skills is None:
            return None
        return [
            {
                "name": s.name,
                "description": s.description,
                "mode": s.mode.value,
                "active": self.skills.is_active(s.name),
            }
            for s in self.skills.list_skills()
        ]

    def reload_skills(self) -> int | None:
        """手动重扫技能（/skill reload 热更新）；重扫后刷新目录块；未启用技能系统为 None。"""
        if self.skills is None:
            return None
        n = self.skills.reload()
        self._skills_catalog_block = self.skills.catalog_block()
        return n

    async def _stop_spinner(self, stop_spin: asyncio.Event, spin: asyncio.Task) -> None:
        """停掉等待 spinner（幂等）：首个可见输出前调用。"""
        if not stop_spin.is_set():
            stop_spin.set()
            await spin

    # ── main loop ──────────────────────────────────────────────────────────────

    async def _fire_hook(self, event: str, **kwargs):
        """触发一个生命周期 Hook 事件（#0013）；无 hooks 时 no-op。绝不上抛（已在 manager 内隔离）。"""
        if self.hooks is None:
            return None
        return await self.hooks.fire(event, **kwargs)

    async def run(self) -> None:
        self._print_banner()
        # 子 Agent 委派（#0014）：把后台管理器绑定到当前主事件循环——spawn_agent 经同步桥
        # （run_coroutine_threadsafe）把子 Agent 协程投递到此循环真并行执行。
        if self.background_manager is not None:
            self.background_manager.bind_loop(asyncio.get_running_loop())
        # 团队功能（#0016）：把唤醒协调器绑定到主循环——队员/Lead 互相唤醒（排 turn）的信号经
        # call_soon_threadsafe 安全置位（队员协程跑在同步桥投递的主循环上，但 send 走工具线程）。
        if self.team_manager is not None:
            self.team_manager.coordinator.bind_loop(asyncio.get_running_loop())
        # Git Worktree 周期清理（#0015）：随主循环启动一个周期任务，回收过期的临时工作树（三层过滤）。
        # 无 worktree 子系统时不挂载。退出时在 finally 取消。
        self._worktree_cleanup_task = (
            asyncio.create_task(self._worktree_cleanup_loop())
            if self.worktree_manager is not None
            else None
        )
        # 记忆初始化（#0010）：清理过期会话 + 续接最近会话 + 组装长期记忆索引（仅首轮注入）。
        await self._init_memory()
        # SessionStart（#0013）：会话启动触发一次；注入文本随首次模型请求经动态通道消费。
        await self._fire_hook(SESSION_START)
        # 退出钩子（#0008）：无论正常退出（/exit、EOF）还是异常 / 中断逃逸，都回收 MCP 子进程。
        try:
            await self._main_loop()
        finally:
            # worktree 周期清理任务（#0015）：退出时取消（best-effort，不阻塞退出）。
            task = getattr(self, "_worktree_cleanup_task", None)
            if task is not None:
                task.cancel()
            # SessionEnd（#0013）：会话退出触发一次（正常退出 / 异常逃逸都经此）。
            await self._fire_hook(SESSION_END)
            await self._flush_memory_task()  # 退出前等最后一轮记忆钩子落定（best-effort）
            self._close_mcp()

    async def _worktree_cleanup_loop(self) -> None:
        """周期回收过期临时工作树（#0015）：每隔固定节奏跑一次三层过滤清理；失败软化、不崩主循环。"""
        from coreagent.worktree.cleanup import cleanup_idle_worktrees
        from coreagent.worktree.constants import CLEANUP_INTERVAL_SECONDS

        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                removed = await asyncio.to_thread(
                    cleanup_idle_worktrees, self.worktree_manager
                )
                if removed:
                    import logging as _logging

                    _logging.getLogger(__name__).info(
                        "worktree 周期清理回收 %d 个过期工作树", len(removed)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 清理失败不崩主循环
                import logging as _logging

                _logging.getLogger(__name__).warning("worktree 周期清理异常：%r", e)

    # ── 记忆系统（#0010）────────────────────────────────────────────────────────────

    async def _init_memory(self) -> None:
        """启动：清理过期会话 + 续接最近会话摘要 + 组装长期记忆索引 → self._memory_block。"""
        if self.session_memory is None:
            return
        try:
            from coreagent.memory import paths as _paths

            mem = self.config.memory
            _paths.cleanup_expired(
                self.session_memory.project_dir,
                mem.retention_days,
                root=self.session_memory.root,
                exclude=self.session_memory.session_id,
            )
            from coreagent.memory import constants as _memc

            sections: list[str] = []
            recovery = await self.session_memory.recover()
            if recovery:
                sections.append(f"{_memc.RESUME_SECTION_HEADER}\n{recovery}")
            if self.notes_store is not None:
                index = self.notes_store.render_index()
                if index:
                    sections.append(f"{_memc.INDEX_SECTION_HEADER}\n{index}")
            self._memory_block = "\n\n".join(sections) if sections else None
        except Exception as e:  # noqa: BLE001 —— 记忆初始化失败软化：空上下文启动，不中断
            import logging

            logging.getLogger(__name__).warning("记忆初始化失败，以空上下文启动：%r", e)
            self._memory_block = None

    def _schedule_memory_hook(self) -> None:
        """每轮自然停下后挂后台钩子：基于本轮历史快照异步更新自动笔记 + 会话摘要。

        读快照、不改 conversation（故不致历史失衡）；与下一轮串行（下轮开跑前先 await 上一钩子）。
        """
        if self.session_memory is None and self.notes_store is None:
            return
        snapshot = self.conversation.get_messages()  # 浅拷贝快照，防钩子与下一轮并发改写
        self._memory_task = asyncio.create_task(self._run_memory_hook(snapshot))

    async def _run_memory_hook(self, snapshot: list[dict]) -> None:
        """后台钩子本体：并发跑「会话摘要存档」+「自动笔记更新」，各自失败软化、互不影响。"""
        tasks = []
        if self.session_memory is not None:
            tasks.append(self.session_memory.update_summary(snapshot))
        if self.notes_store is not None:
            # 复用 #0009 的历史拍平器把本轮快照转纯文本，喂给笔记判定。
            from coreagent.context.summarize import render_history_for_summary

            text = render_history_for_summary(snapshot)
            tasks.append(self.notes_store.update(self.provider, text))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _flush_memory_task(self) -> None:
        """等待挂起的后台记忆钩子完成（幂等）：用于「与下一轮串行」与退出前收尾。"""
        task = self._memory_task
        if task is not None and not task.done():
            try:
                await task
            except Exception:  # noqa: BLE001 —— 钩子异常不影响主流程
                pass
        self._memory_task = None

    def _close_mcp(self) -> None:
        """退出路径回收 MCP 子进程（正常退出 / 中断都经此；幂等，失败不阻断退出）。"""
        if self.mcp_manager is not None:
            try:
                self.mcp_manager.close()
            except Exception:  # noqa: BLE001
                pass

    # ── 命令分发（#0011）──────────────────────────────────────────────────────────

    async def _dispatch_command(self, parsed):
        """据解析结果分发到注册中心的命令处理函数。

        未命中 → 打印 /help 引导后返回 None（主循环 continue）；单条命令内部异常 → 软化为
        一条提示、返回 None，不崩主循环；命中正常 → 返回命令结果（含退出 / 回灌信号）。
        """
        spec = self.commands.resolve(parsed.name)
        if spec is None:
            # 次级来源（#0012）：内置命令未命中时，技能名作为命令分发——`/技能名 [参数]` 触发即激活并执行。
            if self.skills is not None and self.skills.get(parsed.name) is not None:
                try:
                    return await self._dispatch_skill(parsed.name, parsed.args)
                except Exception as e:  # noqa: BLE001 —— 技能触发异常软化为提示，不崩主循环
                    self.console.print(f"  [red]✗[/red]  [dim]技能执行出错：{e}[/dim]")
                    return None
            self.show(unknown_command_message(parsed.name))
            return None
        try:
            return await spec.handler(self, parsed.args)
        except Exception as e:  # noqa: BLE001 —— 单条命令异常软化为提示，不崩主循环
            self.console.print(f"  [red]✗[/red]  [dim]命令执行出错：{e}[/dim]")
            return None

    async def _dispatch_skill(self, name: str, args: str):
        """`/技能名 [参数]` 触发即激活并执行（#0012）。

        共享模式：激活（钉指令 + 收窄工具，激活态持续到清空）→ 回灌触发提示词，主循环跑一轮 AI。
        独立模式：起独立子对话跑到自然结束、回流一条摘要到主历史；不长期钉激活，不触发主循环回合。
        """
        spec = self.skills.get(name)
        if spec is None:
            self.show(unknown_command_message(name))
            return None
        if spec.mode is SkillMode.INDEPENDENT:
            await self._run_independent_skill(spec, args)
            return CommandResult()
        # 共享模式：激活并回灌触发提示词（完整 SOP 由每轮激活指令块承载）。
        self.skills.activate(name, args)
        self.console.print(f"  [cyan]◆[/cyan]  [dim]已激活技能 /{name}（共享模式）[/dim]")
        return CommandResult(prompt=shared_trigger_prompt(name, args))

    async def _run_independent_skill(self, spec, args: str) -> None:
        """在独立子对话里跑一个独立模式技能，回流一条摘要到主历史（带进度渲染与确认 / 门禁）。"""
        await self._flush_memory_task()  # 与后台记忆钩子串行，防并发改写历史
        self.console.print(f"  [cyan]▶[/cyan]  [dim]独立执行技能 /{spec.name}…[/dim]")
        self._in_thinking = False
        self._round_streaming = False
        sys.stdout.write("\n" + format_model_header(spec.model or self.config.model) + "\n")
        sys.stdout.flush()
        # 纯事件消费者：所有事件（含工具调用 / 结果）经单一 on_event 回调交给 _render_event 渲染。
        await run_independent_skill(
            self.provider,
            self.conversation,
            self.registry,
            spec,
            args,
            system=self.system_prompt,
            gate=(self._gate if self.pipeline is not None else None),
            confirm=self._confirm,
            on_event=self._render_event,
            hooks=self.hooks,
        )
        self.conversation.save()
        self.console.print(f"  [green]✓[/green]  [dim]技能 /{spec.name} 执行完毕，摘要已回流主历史[/dim]")

    async def _main_loop(self) -> None:
        while True:
            try:
                # 显式传回主提示符：confirm / plan 复用同一 session 会把 self.message
                # 改成它们的临时文案，不复位则主输入框会残留「⚠ 执行…? [y/N]」。
                # 团队功能开（Lead）时 _read_user_input 还会让「新团队消息」中断输入等待、排一个
                # drain turn（_WAKE 哨兵）；团队功能关时它等价于裸 prompt_async（行为不变）。
                user_input = await self._read_user_input()
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print()
                break

            # Lead 被团队消息唤醒（#0016 T13）：不解析命令，直接排一个 drain turn——team-inbox 块
            # 在本轮装配时承载新消息（运行中的 turn 不受影响，因唤醒只在 Lead 空闲等输入时发生）。
            if user_input is _WAKE:
                user_input = _LEAD_DRAIN_PROMPT
            else:
                user_input = user_input.strip()
                if not user_input:
                    continue

                # ── 命令分流（#0011）：是命令走本地分发；不是命令（或提示词类回灌）才送 AI。──
                parsed = parse(user_input)
                if parsed is not None:
                    result = await self._dispatch_command(parsed)
                    if result is None:
                        continue          # 已在本地处理完（含未命中引导 / 出错软化）
                    if result.exit:
                        self._print_goodbye()
                        break
                    if not result.prompt:
                        continue          # 纯本地 / 影响界面命令：无 AI 回合
                    user_input = result.prompt   # 提示词类：回灌为本轮用户消息，继续跑 Agent 回合

            # 与上一轮后台记忆钩子串行：下一轮开跑前先等其落定（防并发、防竞态）。
            await self._flush_memory_task()

            self.conversation.add_user(user_input)
            # UserPromptSubmit（#0013）：每次用户输入入列时触发；注入文本随本轮请求消费。
            await self._fire_hook(USER_PROMPT_SUBMIT, extra={"prompt": user_input})

            # ── spinner while waiting for the first token ──────────────────────
            stop_spin = asyncio.Event()
            spin = asyncio.create_task(_run_spinner(stop_spin))

            self._in_thinking = False
            self._round_streaming = False
            header_printed = False
            plan_from_loop: list | None = None
            exit_reason: RunEndReason | None = None

            # 流式期间把 Ctrl+C 转为「置取消令牌」，让循环优雅收尾（补齐 tool_result、配平历史），
            # 替代裸 KeyboardInterrupt 冒泡。非 Unix / 无运行 loop 时回落到 KeyboardInterrupt 分支。
            cancel = asyncio.Event()
            loop = asyncio.get_running_loop()
            sigint_installed = False
            try:
                loop.add_signal_handler(signal.SIGINT, cancel.set)
                sigint_installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass

            # 后台子 Agent 结果后缀（#0014 T9）：装配 system 前 drain（消费即清）；本轮经 system
            # 尾部块（不进可缓存前缀）回传给主 Agent，读完即清、不重复注入。无完成结果则为 None。
            background_results_block = (
                self.background_manager.drain_results_block()
                if self.background_manager is not None
                else None
            )
            # 团队邮箱后缀（#0016 T8/T13）：装配前 drain Lead 邮箱（消费即清）→ <team-inbox> 块。
            team_inbox_block = self._drain_lead_inbox()
            # delegate 协调模式工具收窄（#0016 T12）：开启时把 Lead 工具集裁到读类 + shell + 团队工具。
            extra_tool_filter = self._delegate_filter()

            try:
                # 多轮 Agent Loop：每轮 流式(带工具) → 确认 → 分类执行 → 回灌 → 检查终止条件。
                # assistant / tool_result 消息由 run_agent_turn 写入会话历史。本层为**纯事件消费者**。
                async for ev in run_agent_turn(
                    self.provider,
                    self.conversation,
                    self.registry,
                    self.system_prompt,
                    env_block=self.env_block,
                    gate=(self._gate if self.pipeline is not None else None),
                    confirm=self._confirm,
                    cancel=cancel,
                    plan_only=(self.mode == modes.PLAN),
                    context=self.context,
                    instructions_block=self.instructions_block,
                    memory_block=self._memory_block,
                    skills_catalog_block=self._skills_catalog_block,
                    skills=self.skills,
                    hooks=self.hooks,
                    cwd=self._project_root,
                    background_results_block=background_results_block,
                    team_inbox_block=team_inbox_block,
                    extra_tool_filter=extra_tool_filter,
                ):
                    # 首个「模型输出」事件（文本 / 工具调用）才停 spinner、打印模型抬头——
                    # 轮次 / 重试事件瞬时到达，不应提前打断等待动画。
                    if not header_printed and isinstance(ev, (TextDelta, ToolCall)):
                        await self._stop_spinner(stop_spin, spin)
                        # 模型抬头行内追加实时 token 统计（每个用户回合抬头打印时刷新一次）。
                        stat = (
                            self.context.format_stats(self.conversation.get_messages())
                            if self.context is not None
                            else None
                        )
                        sys.stdout.write("\n" + format_model_header(self.config.model, stat) + "\n")
                        sys.stdout.flush()
                        header_printed = True
                    # 终止 / 错误事件可能在无任何内容事件时到达（如首字节前取消），渲染前先停 spinner。
                    if isinstance(ev, (RunDone, RunError)):
                        await self._stop_spinner(stop_spin, spin)
                        if isinstance(ev, RunDone):
                            exit_reason = ev.reason
                            if ev.plan:
                                plan_from_loop = ev.plan

                    self._render_event(ev)

            except KeyboardInterrupt:
                # 回落路径（未能装上 SIGINT handler 时）：等价于取消。
                sys.stdout.write(f"{_RST}\n")
                self.console.print("  [dim]已取消[/dim]\n")
                self._rollback_pending_user()
                continue

            except Exception as e:
                sys.stdout.write(f"{_RST}\n")
                self.console.print(f"  [red]✗[/red]  {e}\n")
                self._rollback_pending_user()
                continue

            finally:
                if sigint_installed:
                    try:
                        loop.remove_signal_handler(signal.SIGINT)
                    except (NotImplementedError, RuntimeError, ValueError):
                        pass
                if not stop_spin.is_set():
                    stop_spin.set()
                if not spin.done():
                    await spin

            self.conversation.save()

            # 后台子 Agent 状态/用量显示（#0014 T12）：一轮结束后，若有后台任务则打一行简报。
            self._render_background_status()

            # 记忆块仅首轮注入一次：本轮用过即清空，后续轮不再重复注入恢复摘要 / 索引。
            self._memory_block = None

            # Stop（#0013）：一次用户输入的循环「自然结束」后触发（取消 / 上限 / 错误轮不触发）。
            if exit_reason is RunEndReason.COMPLETE:
                await self._fire_hook(STOP)

            # 每轮「自然停下」（模型最终回复无 tool_calls）后挂后台记忆钩子：异步更新笔记 + 摘要，
            # 不阻塞下一轮输入；与下一轮串行（下轮开跑前会 _flush）。取消 / 上限 / 错误轮不触发。
            if exit_reason is RunEndReason.COMPLETE:
                self._schedule_memory_hook()

            # plan 模式：循环结束后展示计划列表、征求确认（确认即退出 plan，不自动执行）。
            if self.mode == modes.PLAN and plan_from_loop:
                await self._review_plan(plan_from_loop)
