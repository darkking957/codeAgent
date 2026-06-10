"""结构化（JSON）日志（#0025 T10）：每请求 / 每工具一行 JSON，含 user / session / event / sandbox。

最简方案：不引日志框架——一个 ``log_event(event, **fields)`` 把一条事件 ``json.dumps`` 成单行交给
``logging``，``configure_logging`` 装一个把**消息原样输出**的 handler（消息本身已是 JSON 行）。便于
``grep``/采集；字段自由扩展（user_id / session_id / sandbox / tool / status…）。

单向依赖：仅标准库 + logging；不被引擎 / 工具反向 import（只活在 web 层）。
"""

import json
import logging
import sys

logger = logging.getLogger("coreagent.web")

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """装一个把日志记录的 message **原样**写到 stdout 的 handler（幂等）。

    因 ``log_event`` 已把整条事件序列化为 JSON 字符串作 message，formatter 只取 ``%(message)s``
    即得到「一行 JSON」。同时让 ``coreagent.web.*`` 子 logger（sms / sandbox）经此输出。
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured = True


def log_event(event: str, **fields) -> None:
    """输出一行结构化 JSON 日志：``{"event": <event>, **fields}``。

    约定字段（按需带）：``user``（归属用户 id）/ ``session``（会话 id）/ ``sandbox``（沙箱状态）/
    ``tool``（工具名）/ ``status``（成败）/ ``ms``（耗时）等。值含密码 / 验证码明文者**不得**传入。
    """
    payload = {"event": event}
    payload.update(fields)
    logger.info(json.dumps(payload, ensure_ascii=False))
