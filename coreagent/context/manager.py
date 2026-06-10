"""T7｜管理层编排：每请求前「先第一层卸载、后判第二层摘要」；实时统计；锚点更新；手动触发。

阈值（第二层）：
  - 自动（manual=False）：est ≥ 窗口 − 生效输出上限 − AUTO_MARGIN（宽余量，早压、防估算误差）。
  - 手动（manual=True）：est ≥ 生效输出上限 + MANUAL_MARGIN（窄余量低门槛，用户主动要压几乎随时可压）。
生效输出上限：思考开 = thinking_max_tokens，否则 max_tokens。

可逆兜底：压缩前自动备份完整历史（.bak）；压缩后就地改写并持久化。任一环节异常 → 降级为
不压缩、不崩、不破坏历史配平（tool_use/tool_result 仍配平、可重放）。
"""

import logging
from pathlib import Path

from coreagent.context.constants import AUTO_MARGIN, MANUAL_MARGIN
from coreagent.context.estimate import estimate_input_tokens
from coreagent.context.offload import DEFAULT_OFFLOAD_DIR, offload_conversation
from coreagent.context.summarize import (
    Summarizer,
    build_boundary_message,
    build_summary_message,
    split_keep_recent,
)
from coreagent.conversation import DEFAULT_PATH, Conversation
from coreagent.hooks.models import POST_COMPACT, PRE_COMPACT

logger = logging.getLogger(__name__)


def format_token_stat(current_tokens: int, window_tokens: int) -> str:
    """实时统计字符串：形如 `12.3k / 200k`（当前一位小数、窗口取整）。"""
    return f"{current_tokens / 1000:.1f}k / {window_tokens / 1000:.0f}k"


class ContextManager:
    def __init__(
        self,
        provider,
        config,
        *,
        offload_dir: Path = DEFAULT_OFFLOAD_DIR,
        history_path: Path = DEFAULT_PATH,
        hooks=None,
    ) -> None:
        self.provider = provider
        self.config = config
        self.offload_dir = offload_dir
        self.history_path = Path(history_path)
        # 生命周期 Hook（#0013）：压缩前 PreCompact（可阻断本次压缩）/ 压缩后 PostCompact（可注入恢复上下文）。
        # 缺省 None 时全程 no-op（旧调用 / 旧测试行为不变）。
        self.hooks = hooks
        self.summarizer = Summarizer(provider)
        # token 锚点：上一次请求的服务端输入计数 + 当时消息条数。压缩后失效（置 None）。
        self.anchor_input: int | None = None
        self.anchor_index: int | None = None

    # ── 估算 / 统计 ────────────────────────────────────────────────────────────────

    def _effective_output_limit(self) -> int:
        return (
            self.config.thinking_max_tokens
            if self.config.thinking.enabled
            else self.config.max_tokens
        )

    def estimate(self, messages: list[dict]) -> int:
        return estimate_input_tokens(messages, self.anchor_input, self.anchor_index)

    def stats(self, messages: list[dict]) -> tuple[int, int]:
        """实时统计取值 = (当前估算, 窗口)。"""
        return self.estimate(messages), self.config.context_window

    def format_stats(self, messages: list[dict]) -> str:
        cur, win = self.stats(messages)
        return format_token_stat(cur, win)

    def record_usage(self, input_tokens: int, message_count: int) -> None:
        """DONE 后回写锚点：把服务端真实输入计数与当时消息条数记为新锚点。"""
        self.anchor_input = input_tokens
        self.anchor_index = message_count

    # ── 触发阈值 ──────────────────────────────────────────────────────────────────

    def _threshold(self, manual: bool) -> int:
        if manual:
            # 窄余量低门槛：只要历史超过一个很低的水位就允许压（用户主动要压）。
            return self._effective_output_limit() + MANUAL_MARGIN
        # 宽余量：逼近「窗口 − 输出预留」时提前压，留 AUTO_MARGIN 防估算误差。
        return self.config.context_window - self._effective_output_limit() - AUTO_MARGIN

    # ── 每请求前入口 ──────────────────────────────────────────────────────────────

    async def before_request(self, conversation: Conversation, *, manual: bool = False) -> None:
        """每请求前：先跑第一层卸载（廉价、管单条大小），再按估算判第二层摘要（贵、调 LLM）。

        任一环节异常 → 降级为不压缩、不破坏历史（catch 兜底）。
        """
        try:
            offload_conversation(conversation.messages, self.offload_dir)
        except Exception as e:  # noqa: BLE001 —— 卸载兜底：失败也不阻断后续 / 不破坏历史
            logger.warning("第一层卸载异常，降级跳过：%r", e)

        try:
            est = self.estimate(conversation.messages)
            if est >= self._threshold(manual):
                # PreCompact（#0013，拦截类）：命中拦截 → 跳过本次压缩（历史不变）。
                if await self._precompact_blocked(conversation):
                    logger.info("PreCompact Hook 拦截，跳过本次上下文压缩")
                elif await self._compress(conversation):
                    await self._fire_postcompact(conversation)
        except Exception as e:  # noqa: BLE001 —— 第二层兜底：失败降级为不压缩、历史原样
            logger.warning("第二层摘要异常，降级为不压缩：%r", e)

    async def _precompact_blocked(self, conversation: Conversation) -> bool:
        """PreCompact（拦截类事件）：命中拦截码 → True（跳过本次压缩）。无 hooks 时恒 False。"""
        if self.hooks is None:
            return False
        outcome = await self.hooks.fire(
            PRE_COMPACT, extra={"message_count": len(conversation.messages)}
        )
        return bool(outcome.blocked)

    async def _fire_postcompact(self, conversation: Conversation) -> None:
        """PostCompact（观测 / 可注入）：压缩完成后触发；注入文本随下次请求消费。无 hooks 时 no-op。"""
        if self.hooks is None:
            return
        await self.hooks.fire(
            POST_COMPACT, extra={"message_count": len(conversation.messages)}
        )

    async def _compress(self, conversation: Conversation) -> bool:
        """触发第二层：切分 → 备份 → 摘要 → 就地改写 → 持久化。摘要失败则降级（历史不变）。

        返回是否真正压缩了（供调用方决定是否触发 PostCompact）。
        """
        messages = conversation.messages
        cutoff = split_keep_recent(messages)
        if cutoff <= 0:
            return False  # 无可摘要区间（保留区已涵盖全部 / 无干净边界）→ 跳过

        summary_region = messages[:cutoff]
        keep_region = messages[cutoff:]

        # 不可逆操作先留后路：摘要前备份完整历史（=压缩前全文）。
        self._backup(conversation)

        summary_text = await self.summarizer.summarize(summary_region)
        if summary_text is None:
            return False  # 熔断 / 调用失败 → 降级不压缩（.bak 已存在但无害，历史未改）

        summary_msg = build_summary_message(summary_text, summary_region)
        boundary_msg = build_boundary_message()
        # 就地改写会话（[摘要] + [边界] + 保留区原文）。
        conversation.messages[:] = [summary_msg, boundary_msg] + keep_region
        # 索引整体位移 → 锚点失效，下次 DONE 重新建立。
        self.anchor_input = None
        self.anchor_index = None
        # 压缩后就地持久化。
        conversation.save(self.history_path)
        logger.info("已压缩上下文：摘要 %d 条 → 保留 %d 条", cutoff, len(keep_region))
        return True

    def _backup(self, conversation: Conversation) -> None:
        backup_path = self.history_path.with_name(self.history_path.name + ".bak")
        conversation.save(backup_path)
