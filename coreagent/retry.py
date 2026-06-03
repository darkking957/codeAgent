import asyncio
import sys

from coreagent.providers.base import StreamChunk


async def stream_with_retry(provider, messages, max_retries: int = 3):
    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = 2 ** (attempt - 1)  # 1s / 2s / 4s
            print(f"\n第 {attempt} 次重试（{wait}s 后）…", flush=True)
            await asyncio.sleep(wait)
        try:
            async for chunk in provider.stream_chat(messages):
                yield chunk
            return
        except Exception as e:
            if attempt == max_retries:
                print(f"\nAPI 调用失败：{e}", flush=True)
                raise
