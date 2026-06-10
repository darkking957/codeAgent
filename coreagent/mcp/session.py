"""单 server 会话（#0008 T3 + T4）。

承载一个 MCP server 的完整协议会话：

  T3 —— 三段握手 + 能力协商 / 出站请求 id 异步匹配 + 超时 / dispatcher 三类路由 /
        断连清理 + 进程级有限次重启 / 优雅关闭。
  T4 —— 工具发现（tools/list 分页拼全量）/ 工具调用（tools/call，超大输出截断、区分错误）。

并发模型：会话整体跑在某个事件循环里（管理器的专用线程循环；单测里是 pytest 循环）。
一个后台 reader 任务消费 transport.incoming() 并 dispatch；出站请求登记 id→Future，响应到达
按 id resolve。断连（reader 迭代结束）→ 清理所有挂起请求（以失败收尾）→ supervisor 触发
进程级重启（受 MAX_RESTARTS 约束），超限标记不可用。

握手纪律：握手未完成不发功能请求；只调用 server 在 capabilities 声明过的特性；版本不兼容即终止。
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from coreagent.mcp import constants
from coreagent.mcp.errors import McpError
from coreagent.mcp.jsonrpc import Notification, Request, Response
from coreagent.mcp.transport import Transport

logger = logging.getLogger(__name__)

# 分页保护：避免恶意 / 故障 server 的 nextCursor 永不终止。
_MAX_PAGES = 100
# 重启失败后的退避（秒），避免持续失败时紧密循环耗尽预算。
_RESTART_BACKOFF = 0.2

TransportFactory = Callable[[], Transport]


@dataclass
class ToolCallResult:
    """一次工具调用的归一化结果（已截断）。"""

    text: str
    is_error: bool


class McpSession:
    """单 server 会话。``transport_factory`` 每次产出一个全新传输（供断连重启重建）。"""

    def __init__(
        self,
        name: str,
        transport_factory: TransportFactory,
        *,
        request_timeout: float = constants.REQUEST_TIMEOUT,
        tool_timeout: float = constants.TOOL_TIMEOUT,
        handshake_timeout: float = constants.CONNECT_TIMEOUT,
        max_restarts: int = constants.MAX_RESTARTS,
    ) -> None:
        self.name = name
        self._factory = transport_factory
        self._request_timeout = request_timeout
        self._tool_timeout = tool_timeout
        self._handshake_timeout = handshake_timeout
        self._max_restarts = max_restarts

        self._transport: Transport | None = None
        self._reader_task: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._disconnected = asyncio.Event()

        self._pending: dict[object, asyncio.Future] = {}
        self._next_id = 0

        self._server_version: str | None = None
        self._server_capabilities: dict = {}
        self._tools: list[dict] = []

        self._closing = False
        self._restarts = 0
        self._available = True

    # ── 只读属性 ─────────────────────────────────────────────────────────────
    @property
    def tools(self) -> list[dict]:
        return list(self._tools)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def restarts(self) -> int:
        return self._restarts

    @property
    def server_version(self) -> str | None:
        return self._server_version

    @property
    def capabilities(self) -> dict:
        return dict(self._server_capabilities)

    def supports(self, capability: str) -> bool:
        """server 是否在 capabilities 声明了某特性（如 ``tools``）。"""
        return capability in (self._server_capabilities or {})

    # ── 生命周期 ─────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """首次连接：起传输 + reader → 握手 + 能力协商 → 工具发现。失败抛 McpError。

        首连成功后启动 supervisor 处理后续断连重启。首连失败由上层（管理器）fail-soft 兜底。
        """
        await self._open_once()
        self._supervisor = asyncio.ensure_future(self._supervise())

    async def _open_once(self) -> None:
        """开一次连接：取消旧 reader → 新传输 + reader → 握手 → 发现工具。"""
        # 取消旧 reader（重启场景）。
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        self._disconnected = asyncio.Event()
        self._transport = self._factory()
        await self._transport.start()
        self._reader_task = asyncio.ensure_future(
            self._read_loop(self._transport, self._disconnected)
        )
        await self._handshake()
        await self.discover_tools()

    async def _read_loop(self, transport: Transport, disconnected: asyncio.Event) -> None:
        """消费 transport.incoming() 并 dispatch；迭代结束（断连）→ 清理挂起 + 置断连信号。"""
        try:
            async for msg in transport.incoming():
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— reader 异常不上抛，按断连处理
            logger.debug("MCP server「%s」reader 异常：%r", self.name, e)
        finally:
            self._fail_all_pending(f"MCP server「{self.name}」连接已断开")
            disconnected.set()

    async def _supervise(self) -> None:
        """断连重启监督：断连 → 受 MAX_RESTARTS 约束重启；超限标记不可用。"""
        while not self._closing:
            await self._disconnected.wait()
            if self._closing:
                return
            if self._restarts >= self._max_restarts:
                self._available = False
                logger.warning(
                    "MCP server「%s」断连重启已达上限 %d 次，标记不可用",
                    self.name, self._max_restarts,
                )
                return
            self._restarts += 1
            logger.warning(
                "MCP server「%s」断连，进行第 %d/%d 次进程级重启",
                self.name, self._restarts, self._max_restarts,
            )
            await self._safe_close_transport()
            try:
                await self._open_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 重启失败：触发下一轮（受预算约束）
                logger.warning("MCP server「%s」第 %d 次重启失败：%r", self.name, self._restarts, e)
                await asyncio.sleep(_RESTART_BACKOFF)
                self._disconnected.set()

    async def close(self) -> None:
        """优雅关闭：停 supervisor / reader → 关传输（回收子进程）→ 清理挂起请求。幂等。"""
        self._closing = True
        for task in (self._supervisor, self._reader_task):
            if task is not None and not task.done():
                task.cancel()
        await self._safe_close_transport()
        self._fail_all_pending(f"MCP server「{self.name}」会话已关闭")
        for task in (self._supervisor, self._reader_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._supervisor = None
        self._reader_task = None

    async def _safe_close_transport(self) -> None:
        if self._transport is not None:
            try:
                await self._transport.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("MCP server「%s」关闭传输异常：%r", self.name, e)

    # ── 请求 / 通知 / dispatch ───────────────────────────────────────────────
    async def _request(self, method: str, params: dict | None = None, *, timeout: float) -> dict:
        """发一个出站请求，登记 id→Future，按 id 等响应（带超时）；错误响应 → 抛 McpError。"""
        if self._transport is None:
            raise McpError(f"MCP server「{self.name}」未连接，无法发送请求 {method}")
        # 捕获当前传输并快速失败：断连 / 重启窗口里旧传输已 closed，此时发出的请求没有 reader
        # 兜底，会一直挂到超时。提前拒绝避免无谓的整段超时等待（重启完成后调用方重试即可）。
        transport = self._transport
        if transport.closed.is_set():
            raise McpError(f"MCP server「{self.name}」连接已断开，无法发送请求 {method}")
        rid = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        try:
            await transport.send(Request(id=rid, method=method, params=params))
        except Exception:
            self._pending.pop(rid, None)
            raise
        try:
            resp: Response = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise McpError(f"请求 {method} 超时（超过 {int(timeout)} 秒）") from None
        finally:
            self._pending.pop(rid, None)
        if resp.is_error:
            err = resp.error or {}
            msg = err.get("message") if isinstance(err, dict) else err
            raise McpError(f"请求 {method} 返回错误：{msg}")
        return resp.result or {}

    async def _notify(self, method: str, params: dict | None = None) -> None:
        if self._transport is None:
            raise McpError(f"MCP server「{self.name}」未连接，无法发送通知 {method}")
        await self._transport.send(Notification(method=method, params=params))

    def _dispatch(self, msg) -> None:
        """三类路由：响应→按 id resolve；通知→记日志 no-op；server 请求→记日志忽略。"""
        if isinstance(msg, Response):
            fut = self._pending.pop(msg.id, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            else:
                logger.debug("MCP server「%s」收到无匹配 id 的响应：%r", self.name, msg.id)
        elif isinstance(msg, Notification):
            # 本号仅记日志 / no-op（含 tools/list_changed；动态刷新延后，见 spec Out of Scope）。
            logger.debug("MCP server「%s」收到通知 %s（本号 no-op）", self.name, msg.method)
        elif isinstance(msg, Request):
            # server→client 请求少见；本号暂不支持，记日志忽略（不回 error 以免反向阻塞）。
            logger.debug("MCP server「%s」收到 server 请求 %s（本号暂不支持，忽略）",
                         self.name, msg.method)

    def _fail_all_pending(self, reason: str) -> None:
        """断连 / 关闭时把所有挂起请求以失败收尾（不悬挂协程）。"""
        pending = self._pending
        self._pending = {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(McpError(reason))

    # ── 握手 ─────────────────────────────────────────────────────────────────
    async def _handshake(self) -> None:
        """三段握手：initialize（带最高版本+capabilities）→ 校验版本 → initialized 通知。"""
        result = await self._request(
            "initialize",
            {
                "protocolVersion": constants.PROTOCOL_VERSION,
                "capabilities": constants.CLIENT_CAPABILITIES,
                "clientInfo": {"name": constants.CLIENT_NAME, "version": constants.CLIENT_VERSION},
            },
            timeout=self._handshake_timeout,
        )
        version = result.get("protocolVersion")
        if version not in constants.SUPPORTED_PROTOCOL_VERSIONS:
            raise McpError(
                f"MCP server「{self.name}」协议版本不兼容：server 返回 {version!r}，"
                f"client 支持 {sorted(constants.SUPPORTED_PROTOCOL_VERSIONS)}；已终止连接"
            )
        self._server_version = version
        self._server_capabilities = result.get("capabilities") or {}
        # 握手第三步：通知 server 客户端就绪（此后方可发功能请求）。
        await self._notify(constants.INITIALIZED_NOTIFICATION)

    # ── 工具发现 / 调用（T4）─────────────────────────────────────────────────
    async def discover_tools(self) -> list[dict]:
        """tools/list 拉全量工具清单（处理分页游标）；未声明 tools 能力则跳过。"""
        if not self.supports("tools"):
            logger.debug("MCP server「%s」未声明 tools 能力，跳过工具发现", self.name)
            self._tools = []
            return []
        tools: list[dict] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict = {}
            if cursor:
                params["cursor"] = cursor
            result = await self._request("tools/list", params, timeout=self._request_timeout)
            batch = result.get("tools") or []
            tools.extend(t for t in batch if isinstance(t, dict))
            cursor = result.get("nextCursor")
            pages += 1
            if not cursor or pages >= _MAX_PAGES:
                break
        self._tools = tools
        return tools

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolCallResult:
        """tools/call 执行远端工具；归一化结果（截断 + isError 标记）。"""
        if not self.supports("tools"):
            raise McpError(f"MCP server「{self.name}」未声明 tools 能力，无法调用工具 {tool_name}")
        result = await self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=self._tool_timeout,
        )
        return self._format_result(result)

    def _format_result(self, result: dict) -> ToolCallResult:
        """tools/call 结果 → 文本（拼接 content 块）+ isError；超大输出截断并标注。"""
        is_error = bool(result.get("isError", False))
        content = result.get("content") or []
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    # 非文本块（image / resource 等）：JSON 转储兜底（本号不渲染富内容）。
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        text = constants.truncate_output("\n".join(parts))
        return ToolCallResult(text=text, is_error=is_error)
