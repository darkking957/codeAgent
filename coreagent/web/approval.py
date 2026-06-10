"""执行前审批回调 + 待决信号注册表 + 快照 diff（#0023，人在回路）。

把 #0007 的 ``ConfirmCb`` 在 web 层实现为**真实**的人在回路审批：命中信任集直放，否则向 SSE 流推
一帧审批请求、注册按工具调用标识索引的待决信号、**三路竞速** await（决策回传 / 超时 / 取消令牌）。
任一先到都让等待**确定收敛**——绝不无限挂起：

  - 回传到达 → 按所选档（once/session/persist/reject）；
  - 超时（60s）→ 安全默认 REJECT（不执行）；
  - 取消令牌置位（#0021 断线 / 用户停止）→ 安全默认 REJECT、放行运行协作式收尾。

四档决策的副作用全由**确认回调自身**承担、引擎不感知（延续 #0007 契约、events.py 零改动）：
SESSION 入内存信任集、PERSIST 另追加进 ``SessionRecord.persist_approved``（落 #0022 会话库、跨重启
随会话回来、作用域仅本会话）。信任按**工具名**（粗粒度）。

写 / 改类工具的 diff：执行前后**真实文件快照**对比（``snapshot_file`` + ``build_diff``，由 app 层在
工具调用 / 结果边界调用）；``run_command`` 改文件不产 file diff（归终端输出区）。

单向依赖：本模块依赖标准库 + #0007 的 ``ConfirmDecision`` + #0023 的 web 帧构造器（``web.sse``）；
**绝不**被引擎 / 入口 / profile / 工具反向 import。
"""

import asyncio
import difflib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from coreagent.agent import ConfirmDecision
from coreagent.tools.base import resolve_path
from coreagent.web.sse import approval_decided_frame, approval_request_frame

logger = logging.getLogger(__name__)

# 审批超时（秒）：到点仍无回传 → 按 REJECT（安全默认：不执行）处理。
APPROVAL_TIMEOUT = 60.0

# 写 / 改类工具：执行前后抓真实文件快照出 diff（run_command 不在列，归终端输出区）。
DIFF_TOOLS = frozenset({"write_file", "edit_file"})

# UI 四档决策线上值 → ConfirmDecision（复用 #0007 枚举值；UI「永久」映射到 persist）。
_DECISIONS = {
    "once": ConfirmDecision.ONCE,
    "session": ConfirmDecision.SESSION,
    "persist": ConfirmDecision.PERSIST,
    "reject": ConfirmDecision.REJECT,
}

# 确认回调签名（与 #0007 ConfirmCb 一致）：拿工具调用 dict，返回结构化决策。
ConfirmCb = Callable[[dict], Awaitable[ConfirmDecision]]


def parse_decision(raw: str) -> ConfirmDecision | None:
    """解析四档决策字符串（once/session/persist/reject）；非法值返回 None（端点转 400）。"""
    if not isinstance(raw, str):
        return None
    return _DECISIONS.get(raw)


# ════════════════════════════════════════════════════════════════════════════════
# 待决信号注册表（build_app 作用域、跨并发请求共享）
# ════════════════════════════════════════════════════════════════════════════════

class PendingApprovals:
    """待决审批信号注册表：键 ``(session_id, tool_id)`` → 等待决策的 ``Future``。

    审批 POST 与流式 POST 是**两个并发请求**：流式侧的确认回调 ``register`` 后 await Future；审批侧
    ``resolve`` 定位同一 Future 并 set_result 唤醒它。运行进入任一终态即 ``clear_session`` 移除该会话
    名下全部待决信号——此后对这些标识的回传一律按**过期**处理（``resolve`` 返 False → 端点 404）。
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], asyncio.Future] = {}

    def register(self, session_id: str, tool_id: str) -> asyncio.Future:
        """登记一个待决信号（同 loop 内创建 Future）；同键重复登记直接覆盖（上一个理应已收敛）。"""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[(session_id, tool_id)] = fut
        return fut

    def resolve(self, session_id: str, tool_id: str, decision: ConfirmDecision) -> bool:
        """回传决策：定位 Future 并 set_result；未知 / 已决 / 已过期（已清理）→ 返回 False。"""
        fut = self._pending.get((session_id, tool_id))
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def discard(self, session_id: str, tool_id: str) -> None:
        """单个待决信号收敛后移除（确认回调在 await 结束后调用，配合 clear_session 兜底）。"""
        self._pending.pop((session_id, tool_id), None)

    def clear_session(self, session_id: str) -> None:
        """终态清理：移除该会话名下所有待决信号（之后回传 → 过期 404）。"""
        for key in [k for k in self._pending if k[0] == session_id]:
            self._pending.pop(key, None)

    def has(self, session_id: str, tool_id: str) -> bool:
        return (session_id, tool_id) in self._pending


async def _wait_set(cancel) -> None:
    """等待取消令牌置位。``asyncio.Event`` 直接 await 其 ``wait()``；其它对象退化为轮询 is_set。"""
    if cancel is None:
        await asyncio.Event().wait()  # 永不返回（无取消令牌 → 此路不参与竞速）
        return
    waiter = getattr(cancel, "wait", None)
    if callable(waiter):
        await cancel.wait()
        return
    while not cancel.is_set():
        await asyncio.sleep(0.05)


async def wait_for_decision(
    fut: asyncio.Future, cancel, timeout: float = APPROVAL_TIMEOUT
) -> ConfirmDecision:
    """三路竞速等待：回传 Future / 超时 / 取消令牌置位。任一先到都确定收敛、绝不无限挂起。

    - 回传先到 → 返回回送的决策；
    - 超时 / 取消先到 → 返回 ``REJECT``（安全默认：不执行）。
    """
    cancel_task = asyncio.ensure_future(_wait_set(cancel))
    try:
        done, _ = await asyncio.wait(
            {fut, cancel_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if fut in done and not fut.cancelled():
            return fut.result()
        # 超时（done 为空）或取消令牌先到 → 安全默认拒绝。
        return ConfirmDecision.REJECT
    finally:
        if not cancel_task.done():
            cancel_task.cancel()


# ════════════════════════════════════════════════════════════════════════════════
# per-会话确认回调工厂
# ════════════════════════════════════════════════════════════════════════════════

def make_confirm_cb(
    *,
    session_id: str,
    queue: asyncio.Queue,
    pending: PendingApprovals,
    session_trust: set[str],
    persist_approved: list[str],
    cancel,
    timeout: float = APPROVAL_TIMEOUT,
) -> ConfirmCb:
    """构造 per-会话执行前确认回调闭包（命中审批集的工具执行前由引擎调用）。

    - ``session_trust``：本会话内存信任集（SESSION 决策写入；跨本会话多轮共享）；
    - ``persist_approved``：本会话永久放行工具名（来自 ``SessionRecord``；PERSIST 决策追加，随
      ``pump`` 收尾 save 落库、跨重启回来、作用域仅本会话）；
    - ``cancel``：协作式取消令牌（断线 / 停止），参与三路竞速；
    - ``queue``：SSE 帧队列（推审批请求 / 审批已决帧；引擎事件与 web 帧同队列、消费侧分派）。
    """

    async def confirm(call: dict) -> ConfirmDecision:
        name = call.get("name", "")
        tool_id = call.get("id", "")
        # 信任短路：本会话允许 ∪ 永久允许 → 直接放行，不推审批请求。
        if name in session_trust or name in persist_approved:
            return ConfirmDecision.ONCE

        # 推审批请求帧 + 注册待决信号 + 三路竞速等待。
        fut = pending.register(session_id, tool_id)
        await queue.put(approval_request_frame(tool_id, name, call.get("input") or {}))
        try:
            decision = await wait_for_decision(fut, cancel, timeout)
        finally:
            pending.discard(session_id, tool_id)

        # 副作用由回调自担（引擎不感知）：SESSION 入信任集、PERSIST 另追加永久字段。
        if decision is ConfirmDecision.SESSION:
            session_trust.add(name)
        elif decision is ConfirmDecision.PERSIST:
            session_trust.add(name)
            if name not in persist_approved:
                persist_approved.append(name)

        # 推「审批已决」帧（前端据此关弹窗）；REJECT 也推（运行将据 .approved 跳过该工具）。
        await queue.put(approval_decided_frame(tool_id, decision.value))
        return decision

    return confirm


# ════════════════════════════════════════════════════════════════════════════════
# 执行前后真实文件快照 + 统一 diff
# ════════════════════════════════════════════════════════════════════════════════

def snapshot_file(workspace: str | None, path: str) -> str | None:
    """读取工具将要写的那个文件的当前内容（与工具同样经 ``resolve_path`` 解析 cwd）。

    不存在 / 不可读 → 返回 None（视作「执行前文件不存在」，diff 体现为全新增）。
    """
    if not path:
        return None
    try:
        p = resolve_path(workspace, path)
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def build_diff(path: str, before: str | None, after: str | None) -> str:
    """据**前后真实快照**生成统一 diff 文本（不由工具入参推导）。

    before/after 为 None 视作空串（新建文件 → 全增；删空 → 全删）。无差异 → 返回空串。
    """
    before_lines = (before or "").splitlines(keepends=True)
    after_lines = (after or "").splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines, after_lines, fromfile=f"a/{path}", tofile=f"b/{path}"
    )
    return "".join(diff)


def diff_path(workspace: str | None, path: str) -> Path:
    """暴露快照解析后的绝对路径（便于测试断言抓的是真实文件）。"""
    return resolve_path(workspace, path)
