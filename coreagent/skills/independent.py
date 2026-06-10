"""T8｜独立模式 runner：子对话执行 + 模型自产摘要回流 + token 预算带入近段历史。

流程：
1. 按 frontmatter `max-history-tokens` 预算，从主对话尾部取**近段**历史（总量 ≤ 预算），拍平为
   纯文本作为「参考上下文」（不复用 Summarizer，仅按 token 截取近段——见 spec Out of Scope）。
2. 起一个**独立** Conversation：单条 user 消息 = 参考上下文 + 渲染后 SOP（$ARGUMENTS 已替换）。
3. 以该技能白名单工具（+ 专属工具临时注册）跑 run_agent_turn 到自然结束；可带专属模型覆盖。
4. 取**最后一轮**模型收尾文字为摘要，回流主历史**恰一条** user 消息；不长期钉激活。

边界：独立模式不进技能仓激活集（事后激活态不残留）；专属工具仅在子对话期间临时注册、跑完注销。
"""

import logging

from coreagent.agent import run_agent_turn
from coreagent.context.estimate import message_tokens
from coreagent.context.summarize import render_history_for_summary
from coreagent.conversation import Conversation
from coreagent.events import TextDelta, TextKind, TurnStart
from coreagent.skills import constants
from coreagent.skills.parser import render_body
from coreagent.skills.types import SkillSpec

logger = logging.getLogger(__name__)


def take_recent_within_budget(messages: list[dict], max_tokens: int) -> list[dict]:
    """从尾部取近段消息，使其总近似 token ≤ max_tokens（保序返回，取的是最近的若干条）。

    从最新一条往回累计，加上某条会超预算即停（至少不强行纳入超额条）；空历史返回空列表。
    """
    out: list[dict] = []
    acc = 0
    for msg in reversed(messages):
        t = message_tokens(msg)
        if out and acc + t > max_tokens:
            break
        acc += t
        out.append(msg)
    out.reverse()
    return out


def _register_temp(registry, tools) -> list[str]:
    """临时注册专属工具（子对话期间可见）；返回成功注册的名字（供注销）。"""
    registered: list[str] = []
    for tool in tools:
        registry.unregister(tool.name)
        try:
            registry.register(tool)
            registered.append(tool.name)
        except ValueError as e:
            logger.warning("独立模式专属工具注册失败（跳过）：%s", e)
    return registered


def build_independent_seed(skill: SkillSpec, arguments: str, main_messages: list[dict]) -> str:
    """组装独立子对话的首条 user 消息：近段历史参考上下文 + 渲染后 SOP。"""
    recent = take_recent_within_budget(main_messages, skill.max_history_tokens)
    rendered = render_body(skill.body, arguments)
    if not recent:
        return rendered
    context_text = render_history_for_summary(recent)
    return f"{constants.INDEPENDENT_CONTEXT_HEADER}\n{context_text}\n\n{rendered}"


async def run_independent_skill(
    provider,
    main_conversation: Conversation,
    registry,
    skill: SkillSpec,
    arguments: str = "",
    *,
    system: str | None = None,
    gate=None,
    confirm=None,
    on_event=None,
    hooks=None,
) -> str:
    """跑一个独立模式技能：子对话执行到自然结束，取末轮收尾文字为摘要回流主历史。返回摘要文本。

    本函数是**纯事件消费者**（#0018）：迭代 run_agent_turn 的事件做记账（轮次开始重置末轮缓冲、
    answer 文本累计），并把**所有事件**（含工具调用 / 结果）经单一 ``on_event`` 回调转发给 TUI 渲染。
    """
    seed = build_independent_seed(skill, arguments, main_conversation.get_messages())
    sub = Conversation()
    sub.add_user(seed)

    allow = (
        set(skill.allowed_tools)
        | {t.name for t in skill.dedicated_tools}
        | {constants.LOADER_TOOL_NAME}
    )
    registered = _register_temp(registry, skill.dedicated_tools)

    last_round_text: list[str] = []
    try:
        async for ev in run_agent_turn(
            provider,
            sub,
            registry,
            system,
            gate=gate,
            confirm=confirm,
            allow_tools=allow,
            model_override=skill.model,
            hooks=hooks,
        ):
            # 每轮开始清空：循环结束时 last_round_text 即末轮（自然完成轮）的收尾文字。
            if isinstance(ev, TurnStart):
                last_round_text = []
            elif isinstance(ev, TextDelta) and ev.kind is TextKind.ANSWER:
                last_round_text.append(ev.text)
            if on_event is not None:
                on_event(ev)
    finally:
        for name in registered:
            registry.unregister(name)

    summary = "".join(last_round_text).strip() or "（技能未产出文本摘要）"
    # 回流主历史**恰一条** user 消息（沿用 #0009 摘要为 user 角色的先例，保证后续轮可见）。
    header = constants.INDEPENDENT_SUMMARY_HEADER.format(name=skill.name)
    main_conversation.add_user(f"{header}\n{summary}")
    return summary
