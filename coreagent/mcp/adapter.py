"""适配层：远端工具 → 本仓 Tool + 同步桥（#0008 T6）。

把一个远端 MCP 工具包装为本仓 ``Tool`` 子类，Agent 调用时无感：
  - ``name`` = ``mcp__<server>__<tool>``（加 server 前缀，避免多 server 重名）；
  - ``description`` / ``parameters`` 取自远端发现结果（远端 JSON Schema → 本地参数声明）；
  - ``requires_confirmation = True``（MCP 工具默认需确认，走 agent loop 既有写类确认路径）。

**同步桥**：``Tool.execute`` 是同步契约（registry 会把它丢线程跑）。MCP 会话却跑在另一个
事件循环线程里。``execute`` 经 ``asyncio.run_coroutine_threadsafe`` 把 ``session.call_tool``
协程投递到会话所在循环、阻塞取结果——**不改动** Tool 同步契约 / registry / agent loop。

任何失败（调用错误 / 超时 / 截断）→ 返回 ``ToolResult.fail/ok``，不抛裸异常（延续「失败也是结果」）。
"""

import asyncio
import concurrent.futures
import logging

from coreagent.mcp import constants
from coreagent.mcp.session import McpSession
from coreagent.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# 同步桥外层兜底超时 = 工具调用超时 + 缓冲；让会话内 wait_for 先以精确文案超时返回，
# 本层只兜「会话自身超时也挂死」的极端情形（仿 registry 的整体守卫 > 工具自身超时）。
_BRIDGE_TIMEOUT_BUFFER = 5.0


class McpTool(Tool):
    """远端 MCP 工具的本地包装（一个工具一个实例）。"""

    requires_confirmation = True

    def __init__(
        self,
        server_name: str,
        tool_spec: dict,
        session: McpSession,
        loop: asyncio.AbstractEventLoop,
        *,
        tool_timeout: float = constants.TOOL_TIMEOUT,
    ) -> None:
        self._server_name = server_name
        self._remote_name = str(tool_spec.get("name", ""))
        self.name = constants.tool_full_name(server_name, self._remote_name)
        self.description = (
            str(tool_spec.get("description") or "")
            or f"MCP 工具 {self._remote_name}（来自 server「{server_name}」）"
        )
        # 远端 JSON Schema（inputSchema）→ 本地参数声明；缺省给一个空对象 schema。
        schema = tool_spec.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        self.parameters = schema

        self._session = session
        self._loop = loop
        self._tool_timeout = tool_timeout

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        """同步桥：投递 call_tool 协程到会话循环并阻塞取结果；失败 → ToolResult.fail。

        cwd（#0015）：远端 MCP 工具不在本地文件系统执行，忽略即可（签名兼容 registry 的 kwarg 透传）。
        """
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(self._remote_name, arguments or {}),
                self._loop,
            )
        except RuntimeError as e:  # 循环已停（关闭竞态）
            return ToolResult.fail(f"MCP 工具 {self.name} 调用失败：会话已关闭（{e}）")
        try:
            result = future.result(timeout=self._tool_timeout + _BRIDGE_TIMEOUT_BUFFER)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return ToolResult.fail(
                f"MCP 工具 {self.name} 调用超时（超过 {int(self._tool_timeout)} 秒）"
            )
        except Exception as e:  # noqa: BLE001 —— 任何会话层异常都不许冒泡崩进程
            logger.debug("MCP 工具 %s 调用异常：%r", self.name, e)
            return ToolResult.fail(f"MCP 工具 {self.name} 调用失败：{e}")

        if result.is_error:
            return ToolResult.fail(result.text)
        return ToolResult.ok(result.text)


def build_tools_for_session(
    server_name: str,
    session: McpSession,
    loop: asyncio.AbstractEventLoop,
    *,
    tool_timeout: float = constants.TOOL_TIMEOUT,
) -> list[McpTool]:
    """把会话已发现的工具集逐个包装为 McpTool。"""
    return [
        McpTool(server_name, spec, session, loop, tool_timeout=tool_timeout)
        for spec in session.tools
        if spec.get("name")
    ]
