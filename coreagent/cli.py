"""薄 CLI 事件消费者（#0019）：按 ``--mode chat|code`` 选 profile，纯消费引擎事件极简渲染。

这是一个**独立的**最小消费者，与既有全功能 TUI（`coreagent.tui`）无关、不合并：只为证明
「同一引擎、两种 profile」的两模式行为明显不同——chat 无工具、只问答；code 可读/写/改文件 +
跑命令（写类执行前交互确认）。

定位（最简实现，不照搬 TUI 的富渲染流水线）：
- 解析 ``--mode`` → 建 profile（chat / code）；
- 建 provider / 单会话内存 Conversation / registry；
- ``async for`` 消费 `coreagent.engine.run_profile` 的 #0018 事件，做极简渲染
  （answer 流式打印、工具调用 / 结果各一行提示）；
- CLI 的 confirm = 交互 y/n → `ConfirmDecision`（code 写类执行前问）。

不自行编排多轮循环（那是引擎的事），不影响既有 ``coreagent`` / TUI 入口。
"""

import argparse
import asyncio
import logging
import os
import sys

from coreagent.agent import ConfirmDecision
from coreagent.config import load_config
from coreagent.conversation import Conversation
from coreagent.engine import run_profile
from coreagent.errors import ConfigError
from coreagent.events import (
    Retry,
    RunDone,
    RunEndReason,
    RunError,
    TextDelta,
    TextKind,
    ToolCall,
    ToolResultEvent,
)
from coreagent.profiles import AgentProfile, chat_profile, code_profile
from coreagent.providers import create_provider
from coreagent.tools import build_registry

logger = logging.getLogger(__name__)

# 运行级终止原因 → 一行中文提示。
_END_REASON_TEXT = {
    RunEndReason.COMPLETE: "完成",
    RunEndReason.MAX_TURNS: "达到轮数上限",
    RunEndReason.CANCELLED: "已取消",
}


def _truncate(text: str, limit: int = 200) -> str:
    """工具结果一行提示用：截断过长内容，避免刷屏。"""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _render_event(event) -> None:
    """极简渲染单个引擎事件（answer 流式、工具调用 / 结果各一行；thinking 不渲染）。"""
    if isinstance(event, TextDelta):
        if event.kind == TextKind.ANSWER:
            sys.stdout.write(event.text)
            sys.stdout.flush()
    elif isinstance(event, ToolCall):
        sys.stdout.write(f"\n  → {event.call['name']} {event.call.get('input', {})}\n")
        sys.stdout.flush()
    elif isinstance(event, ToolResultEvent):
        mark = "✗" if event.is_error else "✓"
        sys.stdout.write(f"  {mark} {event.call['name']}：{_truncate(event.result.content)}\n")
        sys.stdout.flush()
    elif isinstance(event, Retry):
        sys.stdout.write(f"\n  ↻ 第 {event.attempt} 次重试将在 {event.wait}s 后发起…\n")
        sys.stdout.flush()
    elif isinstance(event, RunError):
        sys.stdout.write(f"\n  ✗ 运行出错：{event.error_type}\n")
        sys.stdout.flush()
    elif isinstance(event, RunDone):
        # 收尾换行（answer 流式输出后补一个换行，使下一个提示符独占一行）。
        sys.stdout.write("\n")
        if event.reason is not RunEndReason.COMPLETE:
            sys.stdout.write(f"  [{_END_REASON_TEXT.get(event.reason, event.reason)}]\n")
        sys.stdout.flush()


async def _interactive_confirm(call: dict) -> ConfirmDecision:
    """CLI 交互确认：命中审批集的工具执行前问 y/n（同步 input 丢线程，不阻塞事件循环）。

    回车 / 非 y → 拒绝（保守默认）；y / yes → 本次允许（ONCE）。
    """
    prompt = f"  执行 {call['name']} {call.get('input', {})}？[y/N] "
    try:
        answer = await asyncio.to_thread(input, prompt)
    except EOFError:
        return ConfirmDecision.REJECT
    return ConfirmDecision.ONCE if answer.strip().lower() in ("y", "yes") else ConfirmDecision.REJECT


def _build_profile(mode: str, workspace: str) -> AgentProfile:
    """按 mode 建 profile：code 绑工作目录（项目根），chat 无工作目录。"""
    return code_profile(workspace) if mode == "code" else chat_profile()


async def _repl(provider, profile: AgentProfile, registry, *, confirm) -> None:
    """单会话内存 REPL：读一行用户输入 → run_profile 消费事件 → 循环。EOF / 空行退出。"""
    conversation = Conversation()  # 单会话、纯内存、不落盘（不 load / 不 save）
    while True:
        try:
            user_input = await asyncio.to_thread(input, "\n> ")
        except EOFError:
            sys.stdout.write("\n")
            return
        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            return
        conversation.add_user(user_input)
        # 纯事件消费者：引擎负责多轮编排，本层只 async for 转渲染。
        async for event in run_profile(provider, profile, conversation, registry, confirm=confirm):
            _render_event(event)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("COREAGENT_LOG_LEVEL", "WARNING").upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="coreagent.cli",
        description="CoreAgent 薄 CLI —— 同一引擎、两种 profile（chat / code）",
    )
    parser.add_argument("--config", default="config.yaml", help="YAML 配置文件路径")
    parser.add_argument(
        "--mode",
        choices=["chat", "code"],
        default="code",
        help="运行模式：chat（无工具、纯问答）/ code（六工具、可改盘，默认）",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)
    except ConfigError as e:
        print(f"配置错误：{e}")
        sys.exit(1)

    try:
        provider = create_provider(config)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    registry = build_registry()
    profile = _build_profile(args.mode, os.getcwd())
    # 两模式差异可观测：启动横幅打印工具清单（chat 为 0、code 为 6），区别一目了然。
    tool_names = sorted(profile.tools)
    print(
        f"CoreAgent CLI · {args.mode} 模式 · 可用工具 {len(tool_names)} 个"
        + (f"：{', '.join(tool_names)}" if tool_names else "（无工具）")
    )
    # code 模式写类执行前交互确认；chat 无工具，confirm 永不触发（仍传，语义一致）。
    confirm = _interactive_confirm if args.mode == "code" else None
    try:
        asyncio.run(_repl(provider, profile, registry, confirm=confirm))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
