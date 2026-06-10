"""T5｜邮箱 + 消息格式（+ T8 注入块构造）。

每个收件人一个邮箱文件 ``~/.codeagent/teams/{team}/mailbox/{name}.json``（一个消息列表）。写入 /
drain 经 T2 文件锁串行化。消息落盘时**自动补时间戳、默认未读**；字段精确为 ``from`` / ``body`` /
``timestamp`` / ``read`` / ``summary``（+ 可选 ``kind`` 协议类型）。

``drain`` 取未读并标记已读（写回），二次 drain 不重复返回——供 T8「每轮注入消费即清」。注入块
（``<team-inbox>`` 标签）由 ``build_inbox_block`` 构造：仿 #0014 ``<background-agent-results>``，
不进可缓存前缀（anthropic provider 据标签排除 cache_control）。
"""

import time
from pathlib import Path

from coreagent.teams import constants
from coreagent.teams.locking import file_lock, read_json, write_json_atomic
from coreagent.teams.types import Message


def _msg_to_dict(m: Message) -> dict:
    return {
        constants.MSG_FIELD_FROM: m.sender,
        constants.MSG_FIELD_BODY: m.body,
        constants.MSG_FIELD_TIMESTAMP: m.timestamp,
        constants.MSG_FIELD_READ: m.read,
        constants.MSG_FIELD_SUMMARY: m.summary,
        constants.MSG_FIELD_KIND: m.kind,
    }


def _msg_from_dict(d: dict) -> Message:
    return Message(
        sender=d.get(constants.MSG_FIELD_FROM, ""),
        body=d.get(constants.MSG_FIELD_BODY, ""),
        timestamp=d.get(constants.MSG_FIELD_TIMESTAMP, 0.0),
        read=bool(d.get(constants.MSG_FIELD_READ, False)),
        summary=d.get(constants.MSG_FIELD_SUMMARY, ""),
        kind=d.get(constants.MSG_FIELD_KIND),
    )


class Mailbox:
    """一个团队的邮箱集合（按收件人名分文件）。"""

    def __init__(self, team_name: str, root: Path | str | None = None, *, clock=time.time) -> None:
        base = Path(root) if root is not None else constants.TEAMS_DIR
        self.dir = base / team_name / constants.MAILBOX_SUBDIR
        self._clock = clock

    def _path(self, recipient: str) -> Path:
        return self.dir / f"{recipient}.json"

    def deliver(self, recipient: str, message: Message) -> Message:
        """投递一条消息到收件人邮箱（持锁追加）。落盘自动补时间戳、默认未读。"""
        # 落盘前补全：缺时间戳 → 写入侧生成；read 默认未读（构造时即 False，此处不覆盖显式已读）。
        if not message.timestamp:
            message.timestamp = self._clock()
        path = self._path(recipient)
        with file_lock(path):
            existing = read_json(path, default=None)
            items = existing if isinstance(existing, list) else []
            items.append(_msg_to_dict(message))
            write_json_atomic(path, items)
        return message

    def messages(self, recipient: str) -> list[Message]:
        """读收件人全部消息（含已读）；只读、不改状态。"""
        data = read_json(self._path(recipient), default=None)
        if not isinstance(data, list):
            return []
        return [_msg_from_dict(d) for d in data if isinstance(d, dict)]

    def unread_count(self, recipient: str) -> int:
        return sum(1 for m in self.messages(recipient) if not m.read)

    def drain(self, recipient: str) -> list[Message]:
        """取未读消息并标记已读（持锁读改写）；二次 drain 不重复返回（消费即清语义）。"""
        path = self._path(recipient)
        with file_lock(path):
            data = read_json(path, default=None)
            if not isinstance(data, list):
                return []
            unread: list[Message] = []
            changed = False
            for d in data:
                if isinstance(d, dict) and not d.get(constants.MSG_FIELD_READ, False):
                    unread.append(_msg_from_dict(d))
                    d[constants.MSG_FIELD_READ] = True
                    changed = True
            if changed:
                write_json_atomic(path, data)
        return unread


def build_inbox_block(messages: list[Message]) -> str | None:
    """把 drain 出的未读消息拼成带 ``<team-inbox>`` 标签的注入块；空则 None。

    仿 #0014 ``drain_results_block``：来源已 drain（消费即清），故只在下一轮请求出现、读完即清。
    不进可缓存前缀（anthropic provider 据标签排除）。
    """
    if not messages:
        return None
    lines: list[str] = []
    for m in messages:
        tag = f"[{m.kind}] " if m.kind else ""
        lines.append(f"- {tag}来自「{m.sender}」：{m.body}")
    body = "\n".join(lines)
    return (
        f"{constants.INBOX_TAG_OPEN}\n"
        f"以下是发给你的团队消息（读完即处理，勿在回复里复述本块）：\n{body}\n"
        f"{constants.INBOX_TAG_CLOSE}"
    )
