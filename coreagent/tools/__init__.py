"""工具包：集中装配六个核心工具到注册中心。"""

from coreagent.tools.base import Tool, ToolResult
from coreagent.tools.edit_file import EditFileTool
from coreagent.tools.glob_tool import GlobTool
from coreagent.tools.grep_tool import GrepTool
from coreagent.tools.read_file import ReadFileTool
from coreagent.tools.registry import ToolRegistry
from coreagent.tools.run_command import RunCommandTool
from coreagent.tools.write_file import WriteFileTool


def build_registry() -> ToolRegistry:
    """登记六个核心工具，返回就绪的注册中心。"""
    registry = ToolRegistry()
    for tool in (
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        RunCommandTool(),
        GlobTool(),
        GrepTool(),
    ):
        registry.register(tool)
    return registry


__all__ = ["Tool", "ToolResult", "ToolRegistry", "build_registry"]
