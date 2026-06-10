"""短信验证码通道（#0025）：抽象发送接口 + dev 打桩 + 验证码生成/校验/限流。

注册时一次性短信验证码确认手机号。三层：

- **发送接口** ``SmsSender``：把「一条验证码短信」抽象掉。``DevSmsSender`` 打桩（**不真发**，写
  结构化日志、把最近验证码留在内存供端点 dev 回带 / 测试观测）；``TwilioSmsSender`` 是真 provider
  **实现位**（配密钥即填，本期 ``NotImplementedError``）。
- **验证码服务** ``VerificationService``：生成 6 位码、记有效期 / 重发间隔 / 校验尝试 / 每日发送次数，
  全部硬限（钉死值见常量）。状态**内存态**（单实例、短生命周期——码 300s 即过期，重启即清，符合
  「最简优先 + 单实例」）；``now`` 可注入便于确定性测试。

单向依赖：仅标准库 + 本模块日志；**绝不**被引擎 / 入口 / profile / 工具反向 import（只活在 web 层）。
"""

import logging
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("coreagent.web.sms")

# ── 钉死值（checklist）──────────────────────────────────────────────────────────────
CODE_TTL = 300          # 验证码有效期（秒）
RESEND_INTERVAL = 60    # 同号两次发码最小间隔（秒）
MAX_ATTEMPTS = 5        # 单码校验尝试上限（超限作废）
DAILY_SEND_LIMIT = 10   # 每号每自然日发送上限（条）

# 限流 / 校验错误文案（checklist 未钉死，自定，须可观测且明确）。
ERR_TOO_SOON = "发送过于频繁，请 60 秒后再试"
ERR_DAILY_LIMIT = "今日验证码发送已达上限"
ERR_NO_CODE = "请先获取验证码"
ERR_EXPIRED = "验证码已过期，请重新获取"
ERR_WRONG = "验证码不正确"
ERR_TOO_MANY = "验证码尝试次数过多，请重新获取"


class SmsSender(ABC):
    """短信发送接口：把「向某手机号发一条验证码」抽象掉，dev 打桩 / 真 provider 各自实现。"""

    @abstractmethod
    def send(self, phone: str, code: str) -> None:
        """发送一条验证码短信；失败应抛异常（端点转 5xx）。"""


class DevSmsSender(SmsSender):
    """dev 打桩：**不真发**。写结构化日志（验证码可观测）+ 把每号最近验证码留内存（端点 dev 回带）。"""

    def __init__(self) -> None:
        self.last: dict[str, str] = {}  # phone -> 最近一次验证码（仅 dev 观测，绝不用于校验）

    def send(self, phone: str, code: str) -> None:
        self.last[phone] = code
        logger.info(
            "sms_dev_stub_send", extra={"event": "sms_send", "phone": phone, "code": code, "stub": True}
        )


class TwilioSmsSender(SmsSender):
    """真 provider **实现位**（#0025 Out of Scope：不接入真实短信）：配密钥即填。"""

    def __init__(self, account_sid: str = "", auth_token: str = "", from_number: str = "") -> None:
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number

    def send(self, phone: str, code: str) -> None:  # pragma: no cover —— 实现位
        raise NotImplementedError("真实短信 provider 未接入（实现位）：配置密钥后在此调用其 API")


@dataclass
class _Pending:
    """某手机号的当前待校验验证码状态（内存）。"""

    code: str
    expires_at: float
    attempts: int = 0


@dataclass
class _SendQuota:
    """某手机号的发送限流状态（内存）：最近发送时刻 + 当自然日发送计数。"""

    last_send_at: float = 0.0
    day: str = ""
    count: int = 0


class VerificationService:
    """验证码生成 / 校验 / 限流（内存态、单实例）。

    - ``request_code(phone)`` → ``(code, error)``：限流通过则生成 6 位码、经 sender 发出、返回该码
      （端点 dev 模式回带；非 dev 不回带）；被限流则 ``(None, error)``。
    - ``verify(phone, code)`` → ``(ok, error)``：校验有效期 / 尝试上限 / 码是否匹配；成功即作废该码。
    """

    def __init__(self, sender: SmsSender, *, now: Callable[[], float] = time.time) -> None:
        self._sender = sender
        self._now = now
        self._pending: dict[str, _Pending] = {}
        self._quota: dict[str, _SendQuota] = {}

    def _today(self, ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    def request_code(self, phone: str) -> tuple[str | None, str | None]:
        now = self._now()
        q = self._quota.setdefault(phone, _SendQuota())
        # 自然日翻篇 → 计数清零。
        today = self._today(now)
        if q.day != today:
            q.day = today
            q.count = 0
        # 重发间隔 + 每日上限（先判间隔，与 checklist「间隔 <60s 第二次被拒」对齐）。
        if q.last_send_at and now - q.last_send_at < RESEND_INTERVAL:
            return None, ERR_TOO_SOON
        if q.count >= DAILY_SEND_LIMIT:
            return None, ERR_DAILY_LIMIT
        code = f"{secrets.randbelow(10**6):06d}"
        self._pending[phone] = _Pending(code=code, expires_at=now + CODE_TTL)
        q.last_send_at = now
        q.count += 1
        self._sender.send(phone, code)
        logger.info(
            "verification_requested",
            extra={"event": "verification_requested", "phone": phone, "daily_count": q.count},
        )
        return code, None

    def verify(self, phone: str, code: str) -> tuple[bool, str | None]:
        now = self._now()
        pending = self._pending.get(phone)
        if pending is None:
            return False, ERR_NO_CODE
        if now > pending.expires_at:
            self._pending.pop(phone, None)
            return False, ERR_EXPIRED
        if pending.attempts >= MAX_ATTEMPTS:
            self._pending.pop(phone, None)  # 超限即作废
            return False, ERR_TOO_MANY
        pending.attempts += 1
        if not secrets.compare_digest(pending.code, code or ""):
            # 第 5 次错误后作废：本次自增已到上限 → 下次 verify 命中 TOO_MANY；当前返回「不正确」。
            if pending.attempts >= MAX_ATTEMPTS:
                self._pending.pop(phone, None)
                return False, ERR_TOO_MANY
            return False, ERR_WRONG
        # 成功：作废该码（一次性）。
        self._pending.pop(phone, None)
        return True, None
