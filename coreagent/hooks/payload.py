"""事件 → JSON 载荷（#0013 T3）。

命令动作以 JSON 经 stdin 收事件载荷：至少含键 ``event``；工具类事件另含 ``tool_name``、
``tool_input``；其余上下文（如 PermissionDenied 的拒绝理由、PostToolUse 的结果）走 ``extra``
平铺进顶层，供脚本自行解析做细粒度判断。
"""


def build_payload(
    event: str,
    *,
    tool_name: str | None = None,
    tool_input: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """组装一条事件载荷字典（供 JSON 序列化经 stdin 传给命令动作）。"""
    payload: dict = {"event": event}
    if tool_name is not None:
        payload["tool_name"] = tool_name
    if tool_input is not None:
        payload["tool_input"] = tool_input
    if extra:
        # 平铺进顶层（不覆盖 event/tool_name/tool_input 这三个保留键）。
        for k, v in extra.items():
            if k not in ("event", "tool_name", "tool_input"):
                payload[k] = v
    return payload
