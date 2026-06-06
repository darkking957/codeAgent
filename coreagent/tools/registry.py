"""工具注册中心 + 统一执行入口。

职责（仿 `providers/__init__.py: create_provider` 的集中装配风格）：
- 登记工具、按名查找、转成 Anthropic API 认得的工具清单。
- 统一执行：按名分派；整体超时守卫 + 捕获任意异常 → 结构化失败结果（不上抛、不崩溃）。

整体超时守卫说明：execute 把同步工具丢到线程跑并 `wait_for`，既不阻塞事件循环，
又给「忽略自身超时而挂死的工具」兜底。默认 60s 大于 run_command 自身 30s，
故 run_command 会先以自己的精确文案超时返回，不被这里的兜底抢先。
"""

import asyncio
import logging

from coreagent.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# 整体超时守卫（秒）：兜底用，须大于任何工具自身超时。
DEFAULT_TIMEOUT = 60.0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not getattr(tool, "name", ""):
            raise ValueError("工具缺少 name，无法注册")
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

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

    def to_api_tools(self) -> list[dict]:
        """转 Anthropic API 工具清单：name / description / input_schema。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in self._tools.values()
        ]

    async def execute(
        self,
        name: str,
        arguments: dict,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ToolResult:
        """按名分派执行；未知名 / 超时 / 任意异常一律转结构化失败结果。"""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.fail(f"未知工具：{name}")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(tool.execute, arguments), timeout
            )
        except asyncio.TimeoutError:
            logger.warning("工具 %s 执行超过整体守卫 %ss", name, timeout)
            return ToolResult.fail(f"工具 {name} 执行超时（超过 {int(timeout)} 秒）")
        except Exception as e:  # noqa: BLE001 —— 兜底：工具任何异常都不许崩溃
            logger.warning("工具 %s 执行异常：%r", name, e)
            return ToolResult.fail(f"工具 {name} 执行出错：{e}")
