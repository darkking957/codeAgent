"""鉴权核心（#0025）：密码哈希 + 服务端会话签发/校验 + Cookie + 当前用户解析。

手机号 + 密码鉴权，服务端会话 + HttpOnly Cookie。三块：

- **密码哈希**：``hash_password`` / ``verify_password`` 用 **argon2id**（argon2-cffi）；明文只在内存里
  存在于本次请求，**绝不入库、绝不入日志**。最短长度 8（``MIN_PASSWORD_LEN``）。
- **服务端会话**（``SessionTokens``）：登录签发不可猜 token → 内存映射 ``token → (user_id, 过期)``；
  有效期 7 天；登出 ``revoke`` 即失效。**内存态**（单实例，重启即清——符合「最简优先 + 单实例」，
  checklist 不要求登录态跨重启持久，用户 / 会话数据才落库）。``now`` 可注入便于确定性测试。
- **Cookie**：``set_session_cookie`` / ``clear_session_cookie`` 统一 Cookie 属性（``HttpOnly`` /
  ``SameSite=Lax`` / 7 天）；``user_id_from_request`` 从 Cookie 解析当前用户（缺失/失效 → None）。

单向依赖：仅标准库 + argon2 + fastapi 的 ``Request``/``Response``（Cookie 读写本就是 HTTP 概念，
本模块是 web 层）；**绝不**被引擎 / 入口 / profile / 工具反向 import。
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

# 会话 Cookie 名 + 属性（钉死值：HttpOnly / SameSite=Lax / 7 天）。
COOKIE_NAME = "coreagent_session"
SESSION_TTL = 7 * 24 * 3600  # 7 天（秒）
SAMESITE = "Lax"  # 钉死值大小写：Set-Cookie 精确 SameSite=Lax
# 密码最短长度（钉死值）。
MIN_PASSWORD_LEN = 8

# argon2id 哈希器（argon2-cffi 默认算法即 argon2id）；进程级单例（无状态、线程安全）。
_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """argon2id 哈希明文密码，返回自带盐/参数的编码串（直接入库；**绝不**存明文）。"""
    return _hasher.hash(plain)


def verify_password(hashed: str, plain: str) -> bool:
    """校验明文是否匹配哈希；不匹配 / 哈希串非法一律返回 False（不抛、不泄露细节）。"""
    try:
        return _hasher.verify(hashed, plain)
    except (Argon2Error, ValueError, TypeError):
        return False


@dataclass
class _Session:
    user_id: str
    expires_at: float


class SessionTokens:
    """服务端会话 token 注册表（内存、build_app 作用域、跨并发请求共享）。

    ``issue`` 登录签发 → ``resolve`` 受保护端点解析归属用户 → ``revoke`` 登出吊销。过期惰性清理
    （``resolve`` 命中过期即移除并返 None）。``now`` 可注入便于确定性过期测试。
    """

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._sessions: dict[str, _Session] = {}
        self._now = now

    def issue(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = _Session(user_id, self._now() + SESSION_TTL)
        return token

    def resolve(self, token: str | None) -> str | None:
        if not token:
            return None
        s = self._sessions.get(token)
        if s is None:
            return None
        if self._now() > s.expires_at:
            self._sessions.pop(token, None)  # 惰性清理过期
            return None
        return s.user_id

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)


# ── Cookie 读写（统一属性，端点共用）────────────────────────────────────────────────

def set_session_cookie(response, token: str, *, secure: bool = False) -> None:
    """在响应上种会话 Cookie：``HttpOnly`` + ``SameSite=Lax`` + 7 天有效期 + path=/。

    ``secure`` 默认 False（本地 http 闭环 / 测试须能种 Cookie）；生产 https 部署可置 True。
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite=SAMESITE,
        secure=secure,
        path="/",
    )


def clear_session_cookie(response) -> None:
    """登出：删除会话 Cookie（属性须与种植时一致才会被浏览器删除）。"""
    response.delete_cookie(key=COOKIE_NAME, path="/", samesite=SAMESITE)


def token_from_request(request) -> str | None:
    """从请求 Cookie 取会话 token（缺失 → None）。"""
    return request.cookies.get(COOKIE_NAME)
