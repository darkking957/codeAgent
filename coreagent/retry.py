import asyncio
import logging
from typing import AsyncGenerator

from coreagent.events import Retry
from coreagent.providers.base import StreamChunk

logger = logging.getLogger(__name__)

# 瞬时连接/超时类异常的类名（跨 anthropic / openai SDK 与标准库统一识别）。
_RETRYABLE_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectionError",
    "TimeoutError",
}


def _is_retryable(exc: BaseException) -> bool:
    """瞬时错误（连接 / 超时 / 限流 429 / 服务端 ≥500）才可重试；其余不可恢复。"""
    # 1) 连接 / 超时：无 HTTP 状态码，按异常类型识别。
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _RETRYABLE_NAMES:
        return True
    # 2) 带 HTTP 状态码：仅 429（限流）与 5xx（服务端）重试。
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    # 3) 其余（401 / 400 / 403 / 404 / 422 等）不可恢复。
    return False


async def stream_with_retry(
    provider,
    messages,
    *,
    system: str | list | None = None,
    tools: list[dict] | None = None,
    model_override: str | None = None,
    max_retries: int = 3,
) -> AsyncGenerator[StreamChunk | Retry, None]:
    """对瞬时错误退避重试的流式封装。

    - 仅对可重试异常退避：1 / 2 / 4 秒；不可恢复错误立即上抛。
    - 已 yield 过任何 chunk 的尝试若再异常，则不重试、直接上抛（避免重复刷屏）。
    - 重试对用户的可见性经**内联 yield** ``Retry`` 事件（退避 sleep 之前）注入：保留「先提示 →
      再等待 → 后重发」时序；诊断走 logging。消费者据 ``isinstance`` 区分 ``StreamChunk`` 与 ``Retry``。
    - ``system`` / ``tools`` / ``model_override`` 透传给 provider；不传时与纯对话路径行为一致。
    """
    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = 2 ** (attempt - 1)  # 1s / 2s / 4s
            yield Retry(attempt, wait)  # 退避前先提示（单向事件，消费者据类型区分透传）
            logger.warning("第 %d/%d 次重试，%ds 后发起", attempt, max_retries, wait)
            await asyncio.sleep(wait)

        yielded = False
        try:
            async for chunk in provider.stream_chat(
                messages, system=system, tools=tools, model_override=model_override
            ):
                yielded = True
                yield chunk
            return
        except Exception as exc:
            logger.warning("流式第 %d 次尝试失败：%r", attempt + 1, exc)
            if yielded:
                # 本轮已流出内容，重发会重复输出，直接上抛。
                logger.debug("本轮已 yield，停止重试")
                raise
            if attempt >= max_retries or not _is_retryable(exc):
                raise
            # 可重试且仍有次数：进入下一轮。
