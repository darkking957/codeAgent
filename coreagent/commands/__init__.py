"""斜杠命令系统（#0011）：注册中心 + 解析分流 + 界面控制接口。

对外主入口：``build_command_registry()`` 装配登记好十条内置命令（+ 隐藏的 /exit、/mode）的
注册中心；冲突即抛 ValueError（装配层捕获后中文报错退出）。解析分流见 ``parse``；命令向
界面要能力 / 领域数据经 ``UIControl`` 协议。
"""

from coreagent.commands.builtins import register_builtins, unknown_command_message
from coreagent.commands.parser import ParsedCommand, parse
from coreagent.commands.registry import CONFLICT_MSG, CommandRegistry
from coreagent.commands.types import (
    CommandResult,
    CommandSpec,
    CommandType,
    UIControl,
)

__all__ = [
    "build_command_registry",
    "CommandRegistry",
    "CommandSpec",
    "CommandType",
    "CommandResult",
    "UIControl",
    "ParsedCommand",
    "parse",
    "unknown_command_message",
    "CONFLICT_MSG",
]


def build_command_registry() -> CommandRegistry:
    """装配就绪的命令注册中心（登记全部内置命令）。"""
    registry = CommandRegistry()
    register_builtins(registry)
    return registry
