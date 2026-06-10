import asyncio
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
    EXIT_CANCELLED,
    EXIT_CAP,
    run_agent_turn,
)
from coreagent.prompts import build_system_prompt
from coreagent.providers.base import ChunkType
from coreagent.tools import build_registry

# ── ANSI codes for token-by-token streaming ────────────────────────────────────
_AI      = "\033[96m"      # bright cyan  – ◆ model header
_TH_HDR  = "\033[2;36m"   # dim cyan     – ◈ thinking label
_TH_BODY = "\033[2m"      # dim          – thinking body
_SP      = "\033[2;36m"   # dim cyan     – spinner
_RST     = "\033[0m"

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

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


# ── /command completer ─────────────────────────────────────────────────────────
class _CommandCompleter(Completer):
    _CMDS = {
        "/help":  "show commands",
        "/clear": "clear history",
        "/plan":  "toggle plan-only (只规划、不执行写操作)",
        "/exit":  "quit",
        "/quit":  "quit",
    }

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return
        for cmd, meta in self._CMDS.items():
            if cmd.startswith(text):
                yield Completion(
                    cmd[len(text):],
                    display=cmd,
                    display_meta=meta,
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
                 env_block=None):
        self.provider = provider
        self.config = config
        self.conversation = conversation
        # 工具注册中心与系统提示词由 main 注入；缺省时自建，便于测试构造。
        self.registry = registry if registry is not None else build_registry()
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else build_system_prompt(self.registry.names())
        )
        # 环境块（带 <env> 标签）由 main 启动时快照一次注入；缺省 None 时不注入。
        self.env_block = env_block
        self._in_thinking = False
        # plan-only 开关：开启时写类工具被拦截记为计划项、不落盘（见 /plan）。
        self.plan_only = False
        # 本会话信任集（#0005）：选「同意且不再询问」后按工具名记住，仅本会话内存、不落盘；
        # 之后该工具的写操作直接放行、不再弹菜单。
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
            if self.plan_only:
                parts += [
                    ("class:bottom-toolbar.sep",   "  │"),
                    ("class:bottom-toolbar.brand", "  ⏸ plan"),
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
            completer=_CommandCompleter(),
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

    # ── help ───────────────────────────────────────────────────────────────────

    def _print_help(self) -> None:
        grid = Table.grid(padding=(0, 3))
        grid.add_column(style="bright_cyan", min_width=14)
        grid.add_column(style="dim")
        grid.add_row("/help", "show this message")
        grid.add_row("/clear", "clear conversation history")
        grid.add_row("/exit  /quit", "quit CoreAgent")
        grid.add_row("Tab", "complete /commands")
        grid.add_row("↑ ↓", "browse input history")
        grid.add_row("Ctrl+L", "clear screen")
        grid.add_row("Ctrl+C", "interrupt streaming")

        self.console.print()
        self.console.rule("[dim]commands[/dim]", style="dim")
        self.console.print()
        self.console.print("  ", grid)
        self.console.print()
        self.console.rule(style="dim")
        self.console.print()

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

    # ── streaming chunk render ───────────────────────────────────────────────────

    def _render_chunk(self, chunk) -> None:
        """渲染一个流式 chunk；thinking / text / done 纯对话路径行为不变，循环级信号只在多轮可见。"""
        if chunk.type == ChunkType.THINKING:
            if not self._in_thinking:
                sys.stdout.write(f" {_TH_HDR}◈  thinking{_RST}\n{_TH_BODY}")
                self._in_thinking = True
            sys.stdout.write(chunk.content)
            sys.stdout.flush()
        elif chunk.type == ChunkType.TEXT:
            if self._in_thinking:
                sys.stdout.write(f"{_RST}\n")
                self._in_thinking = False
            sys.stdout.write(chunk.content)
            sys.stdout.flush()
        elif chunk.type == ChunkType.DONE:
            if self._in_thinking:
                sys.stdout.write(_RST)
                self._in_thinking = False
            sys.stdout.write("\n\n")
            sys.stdout.flush()
        elif chunk.type == ChunkType.TURN_START:
            # 第 2 轮起显示分组分隔行；单轮纯对话不显示（与 #0003 视觉一致）。
            if chunk.round_index and chunk.round_index >= 2:
                self.console.print(f"[dim]─── 第 {chunk.round_index} 轮 ───[/dim]")
        elif chunk.type == ChunkType.TURN_END:
            pass  # 轮结束无独立可见渲染（stop_reason 仅供上层感知）
        elif chunk.type == ChunkType.LOOP_DONE:
            if chunk.exit_reason == EXIT_CAP:
                self.console.print("  [yellow]●[/yellow]  [dim]已达上限，停止继续调用[/dim]")
            elif chunk.exit_reason == EXIT_CANCELLED:
                self.console.print("  [dim]已取消[/dim]")
            # EXIT_NATURAL：不额外渲染。
        elif chunk.type == ChunkType.LOOP_ERROR:
            self.console.print(f"  [red]✗[/red]  [dim]循环错误：{chunk.error_type}[/dim]")

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

    # ── execute-time confirmation ────────────────────────────────────────────────

    async def _confirm(self, tool_call: dict) -> bool:
        """写类工具执行前的用户确认（#0005 富交互菜单）；三态收敛为二态回传循环。

        流程：非 TTY → 纯文本 y/N 降级（不记忆）；工具名已在本会话信任集 → 直接同意（不弹菜单）；
        否则弹内联富菜单。选「同意且不再询问」→ 把工具名加入信任集后回传同意。循环侧只见 bool。

        不复用主 session（避免再污染 self.message）；富菜单渲染 / 交互全在 confirm_ui 内收敛。
        """
        name = tool_call.get("name", "?")
        # 非 TTY（管道 / 重定向 / CI）→ 纯文本降级，不渲染富 UI、不记忆。
        if not confirm_ui.is_interactive():
            return await confirm_ui.confirm_plain(name)
        # 本会话已信任该工具 → 直接放行。
        if name in self._trusted:
            return True
        preview = confirm_ui.build_preview(tool_call)
        try:
            decision = await confirm_ui.confirm_interactive(preview)
        except (EOFError, KeyboardInterrupt):
            return False
        if decision == confirm_ui.APPROVE_ALWAYS:
            self._trusted.add(name)   # 按名记住（内存、不落盘）
            return True
        return decision == confirm_ui.APPROVE

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
            self.plan_only = False
            self.console.print("  [dim]已退出 plan-only 模式[/dim]")
        else:
            self.console.print("  [dim]仍在 plan-only 模式；下一条输入仍只规划[/dim]")

    async def _stop_spinner(self, stop_spin: asyncio.Event, spin: asyncio.Task) -> None:
        """停掉等待 spinner（幂等）：首个可见输出前调用。"""
        if not stop_spin.is_set():
            stop_spin.set()
            await spin

    # ── main loop ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._print_banner()

        while True:
            try:
                # 显式传回主提示符：confirm / plan 复用同一 session 会把 self.message
                # 改成它们的临时文案，不复位则主输入框会残留「⚠ 执行…? [y/N]」。
                user_input = await self.session.prompt_async(message=_PROMPT_MSG)
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print()
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input in ("/exit", "/quit"):
                self.console.print()
                self.console.print(_cora("bye"))
                self.console.print("\n  [dim]goodbye[/dim]\n")
                break

            if user_input == "/clear":
                self.conversation.clear()
                self.conversation.save()
                self.console.print("  [dim]✓  history cleared[/dim]")
                continue

            if user_input == "/help":
                self._print_help()
                continue

            if user_input == "/plan":
                self.plan_only = not self.plan_only
                if self.plan_only:
                    self.console.print("  [dim]已进入 plan-only 模式（只规划、不执行写操作）[/dim]")
                else:
                    self.console.print("  [dim]已退出 plan-only 模式[/dim]")
                continue

            self.conversation.add_user(user_input)

            # ── spinner while waiting for the first token ──────────────────────
            stop_spin = asyncio.Event()
            spin = asyncio.create_task(_run_spinner(stop_spin))

            self._in_thinking = False
            header_printed = False
            plan_from_loop: list | None = None

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

            try:
                # 多轮 Agent Loop：每轮 流式(带工具) → 确认 → 分类执行 → 回灌 → 检查终止条件。
                # assistant / tool_result 消息由 run_agent_turn 写入会话历史。
                async for chunk in run_agent_turn(
                    self.provider,
                    self.conversation,
                    self.registry,
                    self.system_prompt,
                    env_block=self.env_block,
                    confirm=self._confirm,
                    on_tool=self._render_tool,
                    on_retry=self._on_retry,
                    cancel=cancel,
                    plan_only=self.plan_only,
                ):
                    # 首个「内容」chunk（thinking/text/done）才停 spinner、打印模型抬头——
                    # 循环级信号瞬时到达，不应提前打断等待动画。
                    if not header_printed and chunk.type in (
                        ChunkType.THINKING, ChunkType.TEXT, ChunkType.DONE
                    ):
                        await self._stop_spinner(stop_spin, spin)
                        sys.stdout.write(f"\n {_AI}◆  {self.config.model}{_RST}\n")
                        sys.stdout.flush()
                        header_printed = True
                    # 循环收尾 / 错误信号可能在无任何内容 chunk 时到达，渲染前先停 spinner。
                    if chunk.type in (ChunkType.LOOP_DONE, ChunkType.LOOP_ERROR):
                        await self._stop_spinner(stop_spin, spin)
                        if chunk.type == ChunkType.LOOP_DONE and chunk.plan:
                            plan_from_loop = chunk.plan

                    self._render_chunk(chunk)

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

            # plan-only：循环结束后展示计划列表、征求确认（确认即退出 plan-only，不自动执行）。
            if self.plan_only and plan_from_loop:
                await self._review_plan(plan_from_loop)
