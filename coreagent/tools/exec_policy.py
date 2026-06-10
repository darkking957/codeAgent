"""执行策略通道（#0025）：按请求把「路径约束 + 子进程沙箱」下发给工具，不改工具签名。

#0015 把 ``cwd`` / #0021 把 ``cancel`` 按调用透传给工具；本模块再加一条**按请求**的执行策略通道，
但走 ``ContextVar`` 而非新增 execute kwarg——因 ``asyncio.to_thread`` 会 ``copy_context()`` 把当前
上下文带进工具线程，故在请求任务里 ``use_policy(...)`` 设的策略能传到工具执行处；且各并行子 Agent
各自任务、各自上下文副本，互不串味。**零签名改动 → 零既有工具 mock 破坏**（守「回归不破」）。

策略两面，二者由 web 多用户路径**一并**开启、CLI 路径**不设**（``current_policy()`` 返 None → 旧行为）：

- ``confine_root``：文件类工具（read/write/edit/glob）resolve 后须落此子树内，越界即拒（T6）。
- ``wrap``：子进程类工具（run_command/grep）的沙箱包裹器；不可用即抛 ``SandboxUnavailable``
  → 工具 fail-closed（绝不无沙箱执行，T7）。

本模块是**核心层**：只依赖标准库；沙箱命令的**具体**构造（bwrap）在 ``coreagent/web/sandbox.py``
（web 层）实现并经 ``use_policy`` 注入——工具只认本通道里的 ``wrap`` 回调，**不** import web（守层级）。
"""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class SandboxUnavailable(RuntimeError):
    """需子进程沙箱但探测到沙箱不可用：触发工具 fail-closed（绝不无沙箱执行）。"""


@dataclass(frozen=True)
class WrappedExec:
    """子进程沙箱包裹结果：替换后的 ``argv`` + 可选 ``preexec``（子进程 ``preexec_fn``，设 rlimit）。"""

    argv: list[str]
    preexec: Callable[[], None] | None = None


@dataclass(frozen=True)
class ExecPolicy:
    """一次（per-请求）执行策略：路径约束根 + 子进程沙箱包裹器。

    - ``confine_root``：文件类工具的约束子树根（绝对路径字符串）；None = 不约束。
    - ``wrap``：``(argv, cwd) -> WrappedExec``；不可用应抛 ``SandboxUnavailable``。None = 不沙箱
      （子进程原样跑）——CLI 路径即此。
    """

    confine_root: str | None = None
    wrap: Callable[[list[str], str | None], WrappedExec] | None = None


_POLICY: ContextVar[ExecPolicy | None] = ContextVar("coreagent_exec_policy", default=None)


def current_policy() -> ExecPolicy | None:
    """当前活跃执行策略（无 → None，即 CLITRUST 旧行为）。"""
    return _POLICY.get()


@contextmanager
def use_policy(policy: ExecPolicy | None):
    """在本上下文（及其经 ``to_thread`` 派生的工具线程）内启用一个执行策略；退出即还原。"""
    token = _POLICY.set(policy)
    try:
        yield
    finally:
        _POLICY.reset(token)


def confined_root() -> str | None:
    """当前路径约束根（无策略 / 策略未设约束 → None）。"""
    p = _POLICY.get()
    return p.confine_root if p is not None else None


def wrap_subprocess(argv: list[str], cwd: str | None) -> WrappedExec:
    """据当前策略包裹一条子进程命令：

    - 无策略 / 策略无 ``wrap`` → 原样 ``argv``（不沙箱，CLI 旧行为）；
    - 有 ``wrap`` → 调它（沙箱化）；不可用时由其抛 ``SandboxUnavailable`` → 调用方 fail-closed。
    """
    p = _POLICY.get()
    if p is None or p.wrap is None:
        return WrappedExec(list(argv), None)
    return p.wrap(list(argv), cwd)
