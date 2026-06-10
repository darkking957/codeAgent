"""T8/T9｜后台子 Agent 管理器（真并行 + 状态/结果/用量追踪）+ 系统提示后缀通道。

真并行（T8）：后台子 Agent 经 ``asyncio.run_coroutine_threadsafe`` 投递到**主事件循环**真正并行
推进（仿 #0008 MCP 同步桥），主 Agent 与用户可同时继续。子 Agent 永不弹窗 + 各持私有状态（见
runtime.py），故共享基础设施仅需命名空间隔离即可消除竞态，**无需全局锁**：完成回调由
run_coroutine_threadsafe 经 `call_soon_threadsafe` 在**主循环线程**触发，结果入队与主循环 drain
同线程，天然串行。

系统提示后缀通道（T9）：后台完成 → 摘要入「结果队列」→ 主 Agent 下一轮请求装配 system 时
``drain_results_block()`` 取走并清空（消费即清、不重复注入）。该块带 <background-agent-results>
标签、不进可缓存前缀（见 anthropic provider），不走 Hook 注入旁路、不消耗工具轮次。

会话级追踪：任务 id / 角色 / 类型 / 状态（running/done/failed/cancelled）/ 结果摘要 / 用量。
进程退出即丢（Out of Scope：跨会话持久化）。
"""

import asyncio
import concurrent.futures
import itertools
import logging
import threading
from dataclasses import dataclass, field

from coreagent.agents import constants

logger = logging.getLogger(__name__)

# 任务状态。
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass
class BackgroundTask:
    """一个后台子 Agent 的会话级记录。"""

    agent_id: str
    role: str                       # 角色名（定义式）或 "fork"
    kind: str                       # definitional / fork
    status: str = RUNNING
    summary: str = ""
    usage_input: int = 0
    usage_output: int = 0
    future: concurrent.futures.Future | None = field(default=None, repr=False)

    @property
    def usage_total(self) -> int:
        return self.usage_input + self.usage_output


class BackgroundAgentManager:
    """会话级单例：调度后台子 Agent（真并行）、追踪状态/用量、产出系统提示后缀。"""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        # 结果队列：后台完成的摘要文本，等主循环装配 system 时 drain（消费即清）。
        self._results: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._counter = itertools.count(1)
        # 手动切后台信号（spec 能力 13 第③种进后台）：前台等待轮询它，置位即 detach。
        # 同一时刻至多一个前台子 Agent 在等（主 Agent 串行 await spawn），故单信号足够、无需全局锁。
        self._detach_signal = threading.Event()

    # ── 绑定主循环（TUI run() 启动时调用一次）──────────────────────────────────────
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def next_id(self) -> str:
        """生成下一个子 Agent id（itertools.count.__next__ 在 CPython 原子）。"""
        return f"agent-{next(self._counter)}"

    # ── 手动切后台信号（spec 能力 13 ③）─────────────────────────────────────────────
    def arm_detach(self) -> None:
        """前台等待开始前复位信号（清除上一次的残留请求）。"""
        self._detach_signal.clear()

    def request_detach(self) -> None:
        """请求把当前前台子 Agent 切到后台（TUI 调用；前台等待轮询到即 detach）。"""
        self._detach_signal.set()

    def detach_requested(self) -> bool:
        return self._detach_signal.is_set()

    # ── 调度 / 追踪 ────────────────────────────────────────────────────────────────
    def schedule(self, coro) -> concurrent.futures.Future:
        """把子 Agent 协程投递到主循环真并行执行；返回 concurrent.futures.Future。

        未绑定主循环时抛 RuntimeError（调用方据此降级）。
        """
        if self._loop is None:
            raise RuntimeError("后台事件循环未绑定（请先 bind_loop）")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def track(self, future: concurrent.futures.Future, *, agent_id: str,
              role: str, kind: str) -> BackgroundTask:
        """登记一个后台任务并挂完成回调（完成 → 摘要入结果队列、置终态）。"""
        task = BackgroundTask(agent_id=agent_id, role=role, kind=kind, future=future)
        self._tasks[agent_id] = task
        future.add_done_callback(lambda f: self._on_done(agent_id, f))
        return task

    def _on_done(self, agent_id: str, future: concurrent.futures.Future) -> None:
        """完成回调（主循环线程触发）：读结果 / 异常 → 置状态 + 摘要入队。绝不上抛。"""
        task = self._tasks.get(agent_id)
        if task is None:
            return
        try:
            result = future.result()   # runner 内部已兜底异常，正常返回 SubAgentResult
        except concurrent.futures.CancelledError:
            task.status = CANCELLED
            task.summary = "（后台子 Agent 已取消）"
            self._enqueue(task)
            return
        except Exception as e:  # noqa: BLE001 —— 协程未被 runner 兜底的异常：记 failed、不崩
            logger.warning("后台子 Agent %s 异常：%r", agent_id, e)
            task.status = FAILED
            task.summary = f"（后台子 Agent 执行异常：{type(e).__name__}）"
            self._enqueue(task)
            return
        task.summary = result.summary
        task.usage_input = result.usage_input
        task.usage_output = result.usage_output
        task.status = FAILED if result.error else DONE
        self._enqueue(task)

    def _enqueue(self, task: BackgroundTask) -> None:
        """把一个完成任务的摘要拼成一段文本，入结果队列（供 drain 进系统后缀）。"""
        header = (
            f"后台子 Agent「{task.agent_id}」"
            f"（角色={task.role}，类型={task.kind}，状态={task.status}）已结束"
        )
        self._results.append(f"{header}：\n{task.summary}")

    # ── 系统提示后缀通道（T9）─────────────────────────────────────────────────────
    def drain_results_block(self) -> str | None:
        """取走并清空结果队列，拼成带标签的系统后缀块；无则 None（消费即清、不重复注入）。"""
        if not self._results:
            return None
        body = "\n\n".join(self._results)
        self._results.clear()
        return (
            f"{constants.BACKGROUND_RESULTS_TAG_OPEN}\n"
            f"以下是已在后台完成的子 Agent 结果（仅供参考，读完即处理）：\n{body}\n"
            f"{constants.BACKGROUND_RESULTS_TAG_CLOSE}"
        )

    def has_pending_results(self) -> bool:
        return bool(self._results)

    # ── 只读查询（供 TUI 状态显示）────────────────────────────────────────────────
    def tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def running_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == RUNNING)

    def get(self, agent_id: str) -> BackgroundTask | None:
        return self._tasks.get(agent_id)
