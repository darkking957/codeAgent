"""工具注册中心 + 统一执行入口。

职责（仿 `providers/__init__.py: create_provider` 的集中装配风格）：
- 登记工具、按名查找、转成 Anthropic API 认得的工具清单。
- 统一执行：按名分派；整体超时守卫 + 捕获任意异常 → 结构化失败结果（不上抛、不崩溃）。

整体超时守卫说明：execute 把同步工具丢到线程跑并 `wait_for`，既不阻塞事件循环，
又给「忽略自身超时而挂死的工具」兜底。守卫值须大于任何工具自身超时——故大于
run_command 的硬上限 MAX_TIMEOUT（600s），保证 run_command 总以自己的精确文案超时
返回（含模型经 timeout 放宽到上限的情形），不被这里的兜底抢先。
"""

import asyncio
import contextvars
import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from coreagent.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# 整体超时守卫（秒）：兜底用，须大于任何工具自身超时——含 run_command 的硬上限 600s，
# 故取 660s（留 60s 余量），让 run_command 永远先以自己的精确文案超时返回。
DEFAULT_TIMEOUT = 660.0

# 有界工具执行线程池（#0025 T8）：阻塞型工具走**固定 max_workers** 的池（**非** asyncio.to_thread
# 的随 CPU 浮动默认池），上限可经 env 调；超出即排队，绝不无限扩张线程 / 阻塞事件循环。进程级单例。
# 取 32（固定）：守住「有界」的同时给嵌套场景留足头寸——子 Agent（AgentTool）经同步桥占用一个池线程
# 并阻塞等其子工具，团队/多层委派下需足够并发线程，避免父占满线程、子拿不到线程的自饿死。
_TOOL_MAX_WORKERS = max(1, int(os.environ.get("COREAGENT_TOOL_MAX_WORKERS", "32")))
_TOOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=_TOOL_MAX_WORKERS, thread_name_prefix="coreagent-tool"
)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not getattr(tool, "name", ""):
            raise ValueError("工具缺少 name，无法注册")
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """注销一个工具（动态专属工具注销 / 清空联动用）；存在则移除并返回 True，否则 False。"""
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Tool:
        """按名取工具；未知名抛清楚错误。"""
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"未注册的工具：{name}") from None

    def get_optional(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def to_api_tools(self, allow: set[str] | None = None) -> list[dict]:
        """转 Anthropic API 工具清单：name / description / input_schema。

        ``allow`` 为 None → 全量工具（向后兼容既有调用）；为名字集合 → 仅保留集合内的工具
        （#0012 技能白名单裁剪：集合由技能仓算好，含各激活技能白名单并集 + 系统级 loader）。
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for name, t in self._tools.items()
            if allow is None or name in allow
        ]

    async def execute(
        self,
        name: str,
        arguments: dict,
        *,
        cwd: str | None = None,
        cancel=None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ToolResult:
        """按名分派执行；未知名 / 超时 / 任意异常一律转结构化失败结果。

        ``cwd``（#0015）：本次调用的工作目录，按调用透传给工具（每个 Tool 子类接受 ``cwd`` kwarg，
        路径相关工具据此解析、其余忽略）。缺省 None → 各工具退回进程 cwd（旧行为不变）。

        ``cancel``（#0021）：可选取消令牌（任何带 ``is_set()`` 的对象，如 ``asyncio.Event``），
        与 ``cwd`` 同范式**按调用透传**给工具（不落实例态）。同步快工具忽略；长命令工具
        （run_command）在其轮询循环里查 ``is_set()``，命中即进程组 kill 子孙并返回「已取消」。
        缺省 None → 工具不感知取消（旧行为不变）。
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.fail(f"未知工具：{name}")
        try:
            # 有界池执行（#0025 T8）：不用 asyncio.to_thread（默认无界池）；改 run_in_executor 走有界池。
            # 须**手动 copy_context** 把当前 ContextVar 上下文带进工具线程——执行策略通道（#0025，
            # 路径约束 / 沙箱开关）正是靠它传到工具（to_thread 本会自动 copy，换池后须自己做）。
            loop = asyncio.get_running_loop()
            ctx = contextvars.copy_context()
            call = functools.partial(ctx.run, tool.execute, arguments, cwd=cwd, cancel=cancel)
            return await asyncio.wait_for(loop.run_in_executor(_TOOL_EXECUTOR, call), timeout)
        except asyncio.TimeoutError:
            logger.warning("工具 %s 执行超过整体守卫 %ss", name, timeout)
            return ToolResult.fail(f"工具 {name} 执行超时（超过 {int(timeout)} 秒）")
        except Exception as e:  # noqa: BLE001 —— 兜底：工具任何异常都不许崩溃
            logger.warning("工具 %s 执行异常：%r", name, e)
            return ToolResult.fail(f"工具 {name} 执行出错：{e}")
