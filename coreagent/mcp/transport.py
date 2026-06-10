"""传输抽象 + stdio 子进程传输（#0008 T2）。

传输接口对会话层屏蔽底层是管道还是网络（远程 HTTP 传输延后开号，见 spec Out of Scope）。
本号实现 **stdio 子进程**传输：

  - server 的 **stdout 只走 JSON-RPC**（按行分帧），交协议层解码；
  - server 的 **stderr 不当消息流**——并入本端日志（DEBUG），不污染对话区 / 消息流；
  - 暴露「连接已断」可观测信号（``closed`` 事件 / ``incoming()`` 迭代结束），供会话层清理挂起请求。

设计：``incoming()`` 是异步生成器，逐行读 stdout → 解码 → yield；遇坏行记日志跳过（不杀流），
遇 EOF（进程退出 / 管道关闭）结束迭代并置 ``closed``。
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

from coreagent.mcp import jsonrpc
from coreagent.mcp.errors import McpError
from coreagent.mcp.jsonrpc import JsonRpcDecodeError, Message

logger = logging.getLogger(__name__)

# stdout 行缓冲上限（字节）：单条 JSON-RPC 消息可能很大（工具输出在截断前最高达硬顶 500,000
# 字符）。asyncio 默认 64KB 会对超长行抛 LimitOverrunError，故放宽到远超硬顶的 16MB。
_STREAM_LIMIT = 16 * 1024 * 1024


class Transport(ABC):
    """传输接口：启动、发送一条消息、持续接收、关闭，并暴露断连信号。"""

    @abstractmethod
    async def start(self) -> None:
        """建立底层连接（stdio：起子进程）。失败抛 McpError。"""

    @abstractmethod
    async def send(self, msg: Message) -> None:
        """发送一条消息（编码 + 写出站）。"""

    @abstractmethod
    def incoming(self) -> AsyncIterator[Message]:
        """持续接收消息的异步迭代器；EOF / 断连时结束迭代。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭连接并回收资源（幂等）。"""

    @property
    @abstractmethod
    def closed(self) -> asyncio.Event:
        """断连信号：进程退出 / 管道关闭后被 set。"""


class StdioTransport(Transport):
    """stdio 子进程传输：经 stdin/stdout 管道收发 JSON-RPC，stderr 并入本端日志。"""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.name = name
        self._command = command
        self._args = list(args or [])
        self._env = dict(env or {})
        self._cwd = str(cwd) if cwd else None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closed = asyncio.Event()

    @property
    def closed(self) -> asyncio.Event:
        return self._closed

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    async def start(self) -> None:
        if not self._command:
            raise McpError(f"MCP server「{self.name}」缺少启动命令（command）")
        # 子进程环境 = 当前环境叠加 server 声明的 env（声明值优先）。
        full_env = {**os.environ, **self._env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,  # 捕获后并入本端日志，不混入 stdout 消息流
                env=full_env,
                cwd=self._cwd,
                limit=_STREAM_LIMIT,  # 放宽行缓冲，容纳超大单条消息
            )
        except (OSError, ValueError) as e:
            raise McpError(f"无法启动子进程 {self._command!r}：{e}") from e
        # stderr 后台抽水：逐行并入本端日志（DEBUG），与 stdout 解码流彻底分离。
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.debug(
                    "[mcp:%s stderr] %s", self.name, line.decode("utf-8", "replace").rstrip()
                )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 —— 抽水失败不致命
            pass

    async def send(self, msg: Message) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError(f"MCP server「{self.name}」传输未启动，无法发送")
        data = jsonrpc.encode(msg).encode("utf-8")
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (ConnectionError, BrokenPipeError, OSError) as e:
            self._closed.set()
            raise McpError(f"MCP server「{self.name}」写出站失败（连接已断）：{e}") from e

    async def incoming(self) -> AsyncIterator[Message]:
        if self._proc is None or self._proc.stdout is None:
            raise McpError(f"MCP server「{self.name}」传输未启动，无法接收")
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as e:
                    # 超过行缓冲上限：缓冲已不可恢复，按断连处理（极少见，硬顶远小于上限）。
                    logger.warning("MCP server「%s」单行超出缓冲上限，按断连处理：%s", self.name, e)
                    break
                if not line:  # EOF：进程退出 / 管道关闭
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    yield jsonrpc.decode(text)
                except JsonRpcDecodeError as e:
                    # 坏行不杀流：记日志跳过（stderr 调试文本若误入也在此被吞，不报致命）。
                    logger.warning("MCP server「%s」收到无法解码的行，已跳过：%s", self.name, e)
                    continue
        finally:
            self._closed.set()

    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        proc = self._proc
        if proc is None:
            self._closed.set()
            return
        # 已退出则只回收；否则先 terminate，限时等待，仍不退就 kill。
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
        # 关闭 stdin 管道，避免句柄泄漏。
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        self._closed.set()
