"""JSON-RPC 2.0 三类消息建模 + 编解码（#0008 T1）。

纯协议层，无 I/O：把一条消息建模为请求 / 响应 / 通知三类之一，并提供：
  - ``encode(msg) -> str``：对象 → 单行 JSON 文本（按 stdio 换行分帧，行尾补 ``\\n``）。
  - ``decode(text) -> Request | Response | Notification``：文本 → 分类后的消息对象。

三类判定（见 checklist T1）：
  - 带 id 带 method → **请求**（含 server→client 反向请求，少见）。
  - 带 id 无 method → **响应**（必含 result 或 error 之一）。
  - 无 id 有 method → **通知**（无 id 单向）。

坏 JSON / 缺字段 → 抛 ``JsonRpcDecodeError``（可定位，含原文片段），由上层兜底，不静默吞。
"""

import json
from dataclasses import dataclass
from typing import Union

from coreagent.mcp.errors import McpError

JSONRPC_VERSION = "2.0"


class JsonRpcDecodeError(McpError):
    """解码失败（坏 JSON / 缺字段 / 无法分类）；携带可定位信息。"""


@dataclass
class Request:
    """请求：带 id（关联响应）+ method + 可选 params。"""

    id: Union[int, str]
    method: str
    params: dict | None = None


@dataclass
class Response:
    """响应：带 id（与请求关联）+ result 或 error 之一。"""

    id: Union[int, str, None]
    result: dict | None = None
    error: dict | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


@dataclass
class Notification:
    """通知：无 id 单向 + method + 可选 params。"""

    method: str
    params: dict | None = None


Message = Union[Request, Response, Notification]


def encode(msg: Message) -> str:
    """对象 → 单行 JSON 文本（行尾补换行作 stdio 分帧）。

    ``ensure_ascii=False`` 保留中文；``json.dumps`` 默认不含内嵌换行（字符串内换行被转义为
    ``\\n``），故整条消息恒为单物理行。
    """
    if isinstance(msg, Request):
        obj: dict = {"jsonrpc": JSONRPC_VERSION, "id": msg.id, "method": msg.method}
        if msg.params is not None:
            obj["params"] = msg.params
    elif isinstance(msg, Notification):
        obj = {"jsonrpc": JSONRPC_VERSION, "method": msg.method}
        if msg.params is not None:
            obj["params"] = msg.params
    elif isinstance(msg, Response):
        obj = {"jsonrpc": JSONRPC_VERSION, "id": msg.id}
        if msg.error is not None:
            obj["error"] = msg.error
        else:
            obj["result"] = msg.result
    else:
        raise JsonRpcDecodeError(f"无法编码未知消息类型：{type(msg).__name__}")
    return json.dumps(obj, ensure_ascii=False) + "\n"


def decode(text: str) -> Message:
    """单行 JSON 文本 → 分类后的消息对象；坏 JSON / 缺字段抛可定位错误。"""
    stripped = text.strip()
    if not stripped:
        raise JsonRpcDecodeError("空消息行，无法解码")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise JsonRpcDecodeError(f"JSON 解析失败：{e}；原文片段：{stripped[:200]!r}") from e
    if not isinstance(obj, dict):
        raise JsonRpcDecodeError(f"JSON-RPC 消息须为对象，实为 {type(obj).__name__}")

    has_id = "id" in obj
    has_method = "method" in obj

    # 带 id 带 method → 请求（含 server→client 反向请求）。
    if has_id and has_method:
        return Request(id=obj["id"], method=str(obj["method"]), params=obj.get("params"))
    # 无 id 有 method → 通知。
    if has_method:
        return Notification(method=str(obj["method"]), params=obj.get("params"))
    # 带 id 无 method → 响应（必含 result 或 error 之一）。
    if has_id:
        if "result" not in obj and "error" not in obj:
            raise JsonRpcDecodeError(
                f"响应缺少 result/error 字段；原文片段：{stripped[:200]!r}"
            )
        return Response(id=obj["id"], result=obj.get("result"), error=obj.get("error"))
    raise JsonRpcDecodeError(
        f"无法判定消息类型（既无 method 也无 id）；原文片段：{stripped[:200]!r}"
    )
