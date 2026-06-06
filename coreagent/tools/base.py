"""工具抽象与结构化结果。

设计取舍：
- `Tool` 仿 `providers/base.py: BaseProvider` 用 ABC + abstractmethod 定契约。
- `ToolResult` 仿 `providers/base.py: StreamChunk` 用 dataclass，承载「成功/失败 + 文本」，
  文本即回灌模型的载荷——失败时为错误文案，模型据此重试或调整。
- 工具统一以 `execute(arguments: dict) -> ToolResult` 分派：单一签名规避 LSP 冲突，
  入参形状由 `parameters`（JSON Schema）声明，注册中心据名分派、对异常做结构化兜底。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolResult:
    """工具执行的结构化结果（回灌模型）。

    success=False 时 content 为错误文案；进程不因工具失败崩溃，失败也是一种结果。
    """

    success: bool
    content: str

    @classmethod
    def ok(cls, content: str) -> "ToolResult":
        return cls(True, content)

    @classmethod
    def fail(cls, content: str) -> "ToolResult":
        return cls(False, content)


class Tool(ABC):
    """工具抽象：声明名称 / 描述 / 参数 Schema / 需确认标记，并实现执行方法。

    子类以类属性给出 `name` / `description` / `parameters`，并按需覆盖
    `requires_confirmation`（默认免确认）。
    """

    name: str
    description: str
    # JSON Schema dict：描述 execute 入参形状，转 API 工具清单时作 input_schema。
    parameters: dict
    # 改写类 / 命令类工具置 True，执行前需用户确认；读取类保持 False。
    requires_confirmation: bool = False

    @abstractmethod
    def execute(self, arguments: dict) -> ToolResult:
        """执行工具；任何失败都应返回 ToolResult.fail(...) 而非抛裸异常。"""
        ...
