"""MCP 管理层：事件循环线程 + 连接 + 注册 + 生命周期 + 安全门（#0008 T7 + T8）。

同步桥的「专用线程」一侧：起一个线程跑一个事件循环，承载全部 server 会话（按 server 身份持
长连接、整会话复用）。注册中心本就把工具 ``execute`` 丢线程跑，故适配器阻塞等结果不影响主循环。

启动接入：``build_mcp_manager`` 读分层配置 → 逐 server 过安全门（项目级首次启用确认）→
**fail-soft 阻塞**连接所有已启用 server（带连接超时）：成功的发现工具 → 建适配器 → 注册进
registry；失败 / 拒绝的记中文告警并跳过，Agent 用已连上的工具照常启动。

退出：``close()`` 优雅关闭所有会话、回收子进程、停事件循环线程（接到 TUI 退出路径）。
"""

import asyncio
import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from coreagent.mcp import constants, enablement
from coreagent.mcp.adapter import build_tools_for_session
from coreagent.mcp.config import McpServerConfig, load_mcp_servers
from coreagent.mcp.session import McpSession
from coreagent.mcp.transport import StdioTransport
from coreagent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 连接 server 失败的告警文案（见 checklist 固定值，精确；fail-soft，不崩）。
CONNECT_FAIL_WARNING = "MCP server「{name}」连接失败（{reason}），已跳过；其余工具不受影响"

# 启用确认回调：项目级 server 首次启用时调用，返回是否信任并启动。
ConfirmEnableCb = Callable[[McpServerConfig], bool]


def default_confirm_enable(cfg: McpServerConfig) -> bool:
    """默认启用确认：TTY 下打印固定提示读 y/N；非 TTY（管道 / CI）默认拒绝（fail-closed）。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    prompt = enablement.ENABLE_PROMPT.format(name=cfg.name)
    try:
        ans = input(prompt + " [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return ans.strip().lower() in ("y", "yes")


class McpManager:
    """承载全部 MCP 会话的管理器（专用事件循环线程 + 连接 / 注册 / 关闭）。"""

    def __init__(
        self,
        registry: ToolRegistry,
        servers: dict[str, McpServerConfig],
        *,
        base_dir: Path | str,
        confirm_enable: ConfirmEnableCb = default_confirm_enable,
    ) -> None:
        self._registry = registry
        self._servers = servers
        self._base_dir = Path(base_dir)
        self._confirm_enable = confirm_enable

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._sessions: dict[str, McpSession] = {}
        self._registered_tools: list[str] = []
        self._closed = False

    # ── 事件循环线程 ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """启动专用事件循环线程；阻塞到循环就绪。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="mcp-loop", daemon=True)
        self._thread.start()
        self._started.wait()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _submit(self, coro, timeout: float | None = None):
        """向专用循环投递协程并阻塞取结果（主线程侧调用）。"""
        if self._loop is None:
            raise RuntimeError("MCP 事件循环未启动")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    @property
    def registered_tools(self) -> list[str]:
        return list(self._registered_tools)

    @property
    def sessions(self) -> dict[str, McpSession]:
        return dict(self._sessions)

    # ── 连接所有已启用 server（fail-soft 阻塞）───────────────────────────────
    def connect_all(self) -> None:
        """逐 server 过安全门 + fail-soft 连接；成功的注册工具，失败 / 拒绝的告警跳过。"""
        enabled = enablement.load_enabled(self._base_dir)
        for name, cfg in self._servers.items():
            if not cfg.enabled:
                logger.info("MCP server「%s」已在配置中禁用（enabled=false），跳过", name)
                continue
            if cfg.transport != "stdio":
                # 远程传输延后开号（见 spec Out of Scope）；非 stdio 直接告警跳过。
                logger.warning(
                    CONNECT_FAIL_WARNING.format(
                        name=name, reason=f"暂不支持的传输类型 {cfg.transport!r}（本号仅 stdio）"
                    )
                )
                continue
            if not cfg.command:
                logger.warning(CONNECT_FAIL_WARNING.format(name=name, reason="缺少启动命令 command"))
                continue
            # ① 启用级安全门：仅项目级 server 首次启用弹确认。
            if enablement.needs_enable_confirmation(cfg, enabled):
                if not self._confirm_enable(cfg):
                    logger.warning("项目级 MCP server「%s」未获启用确认，已跳过（其工具不注册）", name)
                    continue
                try:
                    enablement.persist_enabled(name, self._base_dir)
                    enabled.add(name)
                except Exception as e:  # noqa: BLE001 —— 落盘失败不阻断本次启用
                    logger.warning("MCP server「%s」启用集合落盘失败：%s（本次仍启动）", name, e)
            # ② 连接（fail-soft + 连接超时）。
            self._connect_one(cfg)

    def _connect_one(self, cfg: McpServerConfig) -> None:
        name = cfg.name

        def factory():
            return StdioTransport(name, cfg.command, cfg.args, env=cfg.env, cwd=cfg.cwd)

        session = McpSession(name, factory, handshake_timeout=cfg.timeout)
        try:
            # 整个连接（握手 + 发现）受连接超时约束（在循环内 wait_for，外层 result 加缓冲兜底）。
            self._submit(self._do_connect(session, cfg.timeout), timeout=cfg.timeout + 5)
        except Exception as e:  # noqa: BLE001 —— 任何连接失败都 fail-soft：告警 + 跳过
            logger.warning(CONNECT_FAIL_WARNING.format(name=name, reason=self._reason(e)))
            self._safe_close_session(session)
            return
        # 连上：发现的工具 → 适配器 → 注册进 registry。
        self._sessions[name] = session
        self._register_session_tools(name, session, cfg)

    async def _do_connect(self, session: McpSession, timeout: float) -> None:
        """在循环线程内连接，整体受 timeout 约束；失败时清理半成品会话。"""
        try:
            await asyncio.wait_for(session.connect(), timeout)
        except asyncio.TimeoutError:
            await session.close()
            raise
        except Exception:
            await session.close()
            raise

    def _register_session_tools(self, name: str, session: McpSession, cfg: McpServerConfig) -> None:
        assert self._loop is not None
        # 工具调用超时用 TOOL_TIMEOUT（cfg.timeout 是连接超时，二者不同维度）。
        tools = build_tools_for_session(
            name, session, self._loop, tool_timeout=constants.TOOL_TIMEOUT
        )
        if not tools:
            logger.info("MCP server「%s」未发现可用工具", name)
            return
        for tool in tools:
            try:
                self._registry.register(tool)
                self._registered_tools.append(tool.name)
            except ValueError as e:  # 重名等：fail-soft 跳过该工具，不崩
                logger.warning("MCP 工具 %s 注册失败：%s；已跳过", tool.name, e)
        logger.info(
            "MCP server「%s」已注册 %d 个工具：%s",
            name, len(tools), "、".join(t.name for t in tools),
        )

    @staticmethod
    def _reason(exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return "连接超时"
        text = str(exc).strip()
        return text or type(exc).__name__

    def _safe_close_session(self, session: McpSession) -> None:
        try:
            self._submit(session.close(), timeout=5)
        except Exception as e:  # noqa: BLE001
            logger.debug("关闭半成品会话 %s 异常：%r", session.name, e)

    # ── 关闭 / 回收 ──────────────────────────────────────────────────────────
    def close(self) -> None:
        """优雅关闭所有会话、回收子进程、停事件循环线程。幂等。"""
        if self._closed:
            return
        self._closed = True
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            try:
                self._submit(self._close_all_sessions(), timeout=15)
            except Exception as e:  # noqa: BLE001 —— 关闭失败不阻断退出
                logger.debug("关闭 MCP 会话异常：%r", e)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        self._sessions.clear()

    async def _close_all_sessions(self) -> None:
        for session in list(self._sessions.values()):
            try:
                await session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("关闭会话 %s 异常：%r", session.name, e)


def build_mcp_manager(
    registry: ToolRegistry,
    *,
    base_dir: Path | str | None = None,
    confirm_enable: ConfirmEnableCb = default_confirm_enable,
) -> McpManager | None:
    """装配 MCP 管理器：读配置 → 启动循环线程 → fail-soft 连接所有 server → 注册工具。

    无任何 server 配置 → 返回 None（main 据此跳过 MCP 接入，零开销）。
    """
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    servers = load_mcp_servers(base)
    if not servers:
        return None
    manager = McpManager(registry, servers, base_dir=base, confirm_enable=confirm_enable)
    manager.start()
    manager.connect_all()
    return manager
