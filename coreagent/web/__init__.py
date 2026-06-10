"""双模式 Web 层：FastAPI 薄驱动 + 内联 HTML（#0020 起步，#0021 升级为流式 SSE）。

承接 #0019：引擎已收口成「接收 profile 跑、yield 事件、对前端无感知」。本包是其上一层**最薄**的
HTTP 驱动——按 mode 选 chat/code profile，把引擎 #0018 事件**逐个**经 SSE 帧推给前端（#0021，
supersedes #0020 的非流式一次性返回），并支持「停止生成」（断开 → 协作式取消 → 进程组 kill）。
单向依赖：本包 import #0019 入口与 profile、#0021 序列化（web.sse）；引擎/入口/工具**绝不**反向 import 本包。
"""

from coreagent.web.app import build_app, create_app

__all__ = ["build_app", "create_app"]
