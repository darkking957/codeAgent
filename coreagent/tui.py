import asyncio
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

from coreagent.providers.base import ChunkType
from coreagent.retry import stream_with_retry

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


# ── /command completer ─────────────────────────────────────────────────────────
class _CommandCompleter(Completer):
    _CMDS = {
        "/help":  "show commands",
        "/clear": "clear history",
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
    def __init__(self, provider, config, conversation):
        self.provider = provider
        self.config = config
        self.conversation = conversation
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
            parts += [
                ("class:bottom-toolbar.sep",  "  │"),
                ("class:bottom-toolbar.hint", "  Tab  ↑↓  /help "),
            ]
            return FormattedText(parts)

        self.session = PromptSession(
            message=FormattedText([("class:prompt", " ◇  ")]),
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

    # ── main loop ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._print_banner()

        while True:
            try:
                user_input = await self.session.prompt_async()
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

            self.conversation.add_user(user_input)

            # ── spinner while waiting for the first token ──────────────────────
            stop_spin = asyncio.Event()
            spin = asyncio.create_task(_run_spinner(stop_spin))

            text_parts: list[str] = []
            think_parts: list[str] = []
            final_blocks = None
            in_thinking = False
            first_token = False

            try:
                async for chunk in stream_with_retry(
                    self.provider,
                    self.conversation.get_messages(),
                    on_retry=self._on_retry,
                ):
                    if not first_token:
                        stop_spin.set()
                        await spin
                        sys.stdout.write(
                            f"\n {_AI}◆  {self.config.model}{_RST}\n"
                        )
                        sys.stdout.flush()
                        first_token = True

                    if chunk.type == ChunkType.THINKING:
                        if not in_thinking:
                            sys.stdout.write(
                                f" {_TH_HDR}◈  thinking{_RST}\n{_TH_BODY}"
                            )
                            sys.stdout.flush()
                            in_thinking = True
                        sys.stdout.write(chunk.content)
                        sys.stdout.flush()
                        think_parts.append(chunk.content)

                    elif chunk.type == ChunkType.TEXT:
                        if in_thinking:
                            sys.stdout.write(f"{_RST}\n")
                            in_thinking = False
                        sys.stdout.write(chunk.content)
                        sys.stdout.flush()
                        text_parts.append(chunk.content)

                    elif chunk.type == ChunkType.DONE:
                        if in_thinking:
                            sys.stdout.write(_RST)
                        sys.stdout.write("\n\n")
                        sys.stdout.flush()
                        if chunk.blocks is not None:
                            final_blocks = chunk.blocks

            except KeyboardInterrupt:
                sys.stdout.write(f"{_RST}\n")
                self.console.print("  [dim]interrupted[/dim]\n")
                if (
                    self.conversation.messages
                    and self.conversation.messages[-1]["role"] == "user"
                ):
                    self.conversation.messages.pop()
                continue

            except Exception as e:
                sys.stdout.write(f"{_RST}\n")
                self.console.print(f"  [red]✗[/red]  {e}\n")
                if (
                    self.conversation.messages
                    and self.conversation.messages[-1]["role"] == "user"
                ):
                    self.conversation.messages.pop()
                continue

            finally:
                if not stop_spin.is_set():
                    stop_spin.set()
                if not spin.done():
                    await spin

            text = "".join(text_parts)
            thinking = "".join(think_parts)
            self.conversation.add_assistant(text, thinking, blocks=final_blocks)
            self.conversation.save()
