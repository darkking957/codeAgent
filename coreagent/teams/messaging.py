"""T7｜点对点消息投递（名称注册表 + 写邮箱 + 广播 + 协议消息 + 唤醒）。

两段式：从 T3 团队配置的 members 数组（名称注册表）按名字查到收件人 → 写其 T5 邮箱。Lead 亦为
合法收件人（队员的 idle 通知发给它）。查无此人报错。

广播 = **逐个收件人各发一条**（循环单发，不做群发体）。协议消息经 ``kind`` 字段携带
（``approval-request`` / ``approval-reply`` / ``shutdown`` / ``idle``）。

唤醒（spec 能力 13）：投递后若收件人**空闲** → 给它**排一个 turn**（``Coordinator.request_wake``，
置 wake 信号）；收件人**正在跑 turn**（busy）→ 只投递、**绝不**打断当前 turn（不置信号、不碰任何
取消令牌）。split-pane 的窗格 id 唤醒此处占位（本期 in-process 走 wake 信号）。
"""

import asyncio

from coreagent.teams import constants
from coreagent.teams.mailbox import Mailbox
from coreagent.teams.types import Message, Team


class UnknownRecipient(Exception):
    """按名字在名称注册表（members + lead）里查无此收件人。"""


class _Sig:
    """一个成员的唤醒信号：idle 标记 + wake 事件（asyncio.Event）。"""

    __slots__ = ("idle", "wake")

    def __init__(self) -> None:
        self.idle = False
        self.wake = asyncio.Event()


class Coordinator:
    """团队成员唤醒协调（spec 能力 13）：每成员 idle 标记 + wake 信号。

    唤醒空闲成员 = set 其 wake 事件（= 给它排一个 turn）；成员 busy 时不置信号、当前 turn 不受影响
    （成员**仅在 idle 时** ``await wait_wake``）。跨线程置位经绑定主循环 ``call_soon_threadsafe``。
    """

    def __init__(self) -> None:
        self._signals: dict[str, _Sig] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _sig(self, name: str) -> _Sig:
        sig = self._signals.get(name)
        if sig is None:
            sig = _Sig()
            self._signals[name] = sig
        return sig

    def mark_idle(self, name: str) -> None:
        self._sig(name).idle = True

    def mark_busy(self, name: str) -> None:
        sig = self._sig(name)
        sig.idle = False
        sig.wake.clear()

    def is_idle(self, name: str) -> bool:
        return self._sig(name).idle

    def request_wake(self, name: str) -> None:
        """给成员排一个 turn（置 wake 信号）；绝不打断运行中的 turn。线程安全。"""
        sig = self._sig(name)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(sig.wake.set)
        else:
            sig.wake.set()

    def wake_pending(self, name: str) -> bool:
        return self._sig(name).wake.is_set()

    async def wait_wake(self, name: str) -> None:
        """空闲成员 park 在此，被 request_wake 唤醒后清信号继续。"""
        sig = self._sig(name)
        await sig.wake.wait()
        sig.wake.clear()


class Messenger:
    """团队消息投递（持团队名册 + 邮箱 + 唤醒协调器）。"""

    def __init__(self, team: Team, mailbox: Mailbox, coordinator: Coordinator | None = None) -> None:
        self.team = team
        self.mailbox = mailbox
        self.coordinator = coordinator

    def _valid_recipients(self) -> set[str]:
        """名称注册表：members 数组 + Lead（队员的 idle 通知发给 Lead）。"""
        names = set(self.team.member_names())
        if self.team.lead:
            names.add(self.team.lead)
        return names

    def send(
        self,
        sender: str,
        recipient: str,
        body: str,
        *,
        kind: str | None = None,
        summary: str = "",
    ) -> Message:
        """按名字投递一条消息；查无此人抛 UnknownRecipient。投递后按空闲与否决定是否排 turn。"""
        if recipient not in self._valid_recipients():
            known = "、".join(sorted(self._valid_recipients())) or "（无）"
            raise UnknownRecipient(f"查无收件人「{recipient}」。已知：{known}")
        msg = Message(sender=sender, body=body, kind=kind, summary=summary)
        self.mailbox.deliver(recipient, msg)
        # 唤醒：仅当收件人空闲才排一个 turn；运行中绝不打断。
        if self.coordinator is not None and self.coordinator.is_idle(recipient):
            self.coordinator.request_wake(recipient)
        return msg

    def broadcast(
        self,
        sender: str,
        body: str,
        *,
        kind: str | None = None,
        exclude: set[str] | None = None,
    ) -> list[Message]:
        """广播 = 逐个收件人各发一条（循环单发）。返回已发出的消息列表。"""
        exclude = exclude or set()
        out: list[Message] = []
        for name in sorted(self._valid_recipients()):
            if name == sender or name in exclude:
                continue
            out.append(self.send(sender, name, body, kind=kind))
        return out

    # ── 协议消息便捷封装（kind 取值钉 constants）─────────────────────────────────
    def send_approval_request(self, sender: str, lead: str, plan: str) -> Message:
        return self.send(sender, lead, plan, kind=constants.KIND_APPROVAL_REQUEST)

    def send_approval_reply(self, lead: str, member: str, body: str) -> Message:
        return self.send(lead, member, body, kind=constants.KIND_APPROVAL_REPLY)

    def send_shutdown(self, sender: str, recipient: str, body: str = "") -> Message:
        return self.send(sender, recipient, body, kind=constants.KIND_SHUTDOWN)

    def send_idle(self, member: str, lead: str, body: str = "") -> Message:
        return self.send(member, lead, body or f"队员「{member}」已空闲", kind=constants.KIND_IDLE)
