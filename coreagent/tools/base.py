"""工具抽象与结构化结果。

设计取舍：
- `Tool` 仿 `providers/base.py: BaseProvider` 用 ABC + abstractmethod 定契约。
- `ToolResult` 仿 `providers/base.py: StreamChunk` 用 dataclass，承载「成功/失败 + 文本」，
  文本即回灌模型的载荷——失败时为错误文案，模型据此重试或调整。
- 工具统一以 `execute(arguments: dict) -> ToolResult` 分派：单一签名规避 LSP 冲突，
  入参形状由 `parameters`（JSON Schema）声明，注册中心据名分派、对异常做结构化兜底。
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from coreagent.tools.exec_policy import confined_root


class PathEscapeError(Exception):
    """文件类工具解析后的路径逃出活跃约束根（#0025 T6）：工具据此返回明确「越界」错误。"""


def resolve_path(cwd: str | None, path: str) -> Path:
    """路径解析共享 helper（#0015）：绝对路径原样，相对路径挂到 cwd 下。

    cwd 为 None / 空时退回进程 cwd（即原样 ``Path(path)``，保持旧行为 / 旧测试不变）。
    cwd 必须**按每次调用**传入——并行子 Agent 共享工具实例，绝不由实例态承载（会串味）。
    """
    p = Path(path)
    if p.is_absolute() or not cwd:
        return p
    return Path(cwd) / p


def resolve_confined(cwd: str | None, path: str) -> Path:
    """``resolve_path`` + 活跃约束根校验（#0025 T6）：约束根存在且解析后逃出其子树 → 抛
    ``PathEscapeError``；无约束（CLI / 无策略）→ 行为同 ``resolve_path``（旧路径不变）。

    逃逸判定取 **realpath**（``os.path.realpath``，跟随符号链接 + 归一 ``..``，对不存在的新文件也成立）
    后要求落在约束根 realpath 之内——故 ``../别处`` / 绝对 ``/etc/passwd`` / 符号链接逃逸一律被拒。
    """
    p = resolve_path(cwd, path)
    root = confined_root()
    if root is None:
        return p
    root_real = os.path.realpath(root)
    target_real = os.path.realpath(str(p))
    if target_real != root_real and not target_real.startswith(root_real + os.sep):
        raise PathEscapeError(f"路径越界：拒绝访问 workspace 子树外路径：{path}")
    return p


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
    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        """执行工具；任何失败都应返回 ToolResult.fail(...) 而非抛裸异常。

        ``cwd``（#0015）：本次调用的工作目录，**按调用透传**（不落实例态）。路径相关工具用
        ``resolve_path(cwd, path)`` 解析；不涉路径的工具忽略即可（签名须兼容 registry 的 kwarg 调用）。
        缺省 None → 退回进程 cwd（旧行为不变）。

        ``cancel``（#0021）：可选取消令牌（任何带 ``is_set()`` 的对象），同 ``cwd`` **按调用透传**。
        同步快工具**默认忽略**（签名兼容 registry 的 kwarg 调用即可）；只有需要中途响应取消的长命令工具
        （run_command）在其轮询循环里查 ``is_set()`` → 进程组 kill + 返回「已取消」。缺省 None → 不感知。
        """
        ...
