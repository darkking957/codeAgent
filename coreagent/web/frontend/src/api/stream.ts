// SSE 流式消费（#0026 T6，沿用 #0021 客户端机制）：POST 流式端点 → 手解 SSE 帧 → 回调分派。
// 停止 = AbortController.abort() → 断开连接 → 后端轮询感知 → 协作式取消 + 收尾 save。
// 坏帧 try-catch 跳过，不中断整流（沿用原前端容错）。

import type { SSEFrame } from "./types";

export interface StreamHandle {
  /** 中止当前流（停止生成）。 */
  abort: () => void;
  /** 流完整结束 / 被中止后 resolve（用于收尾拉权威历史）。 */
  done: Promise<void>;
}

/** 把一个原始 SSE 帧文本解析为 `{event, data}`；无 event 名或坏 JSON → null。 */
function parseFrame(raw: string): SSEFrame | null {
  let name: string | null = null;
  let dataLine = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLine += line.slice(5).trim();
  }
  if (!name) return null;
  try {
    const data = dataLine ? JSON.parse(dataLine) : {};
    return { event: name, data } as SSEFrame;
  } catch {
    return null; // 坏帧跳过
  }
}

/**
 * POST `url`（带 body）开流，逐帧把解析后的 SSE 帧喂给 `onFrame`。
 * 返回 handle：`abort()` 停止、`done` 在结束后 resolve（无论正常 / 中止 / 出错）。
 */
export function streamMessages(
  url: string,
  body: unknown,
  onFrame: (frame: SSEFrame) => void
): StreamHandle {
  const ctrl = new AbortController();

  const done = (async () => {
    try {
      const resp = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: body != null ? { "Content-Type": "application/json" } : undefined,
        body: body != null ? JSON.stringify(body) : null,
        signal: ctrl.signal,
      });
      if (!resp.body) return;
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done: streamDone, value } = await reader.read();
        if (streamDone) break;
        buf += dec.decode(value, { stream: true });
        let i: number;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const frame = parseFrame(buf.slice(0, i));
          buf = buf.slice(i + 2);
          if (frame) {
            try {
              onFrame(frame);
            } catch (e) {
              console.warn("帧处理失败，跳过", e);
            }
          }
        }
      }
    } catch (e) {
      if ((e as Error)?.name !== "AbortError") console.warn("流连接出错", e);
    }
  })();

  return { abort: () => ctrl.abort(), done };
}
