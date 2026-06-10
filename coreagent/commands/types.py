"""T1｜契约层：命令类型 / 元数据 / 结果 / 界面控制接口（#0011）。

把「命令是什么、执行后给主循环什么控制信号、命令向界面要哪些能力 / 领域数据」固化为
四个契约，使命令逻辑与底层渲染框架（终端富文本 / 输入补全库）解耦——命令只依赖本模块的
``UIControl`` 协议，换渲染层只改协议实现、不改命令。

Enum + dataclass + Protocol 范式对齐既有模块（permissions.rules.Decision/Rule、
agent.ConfirmDecision、providers.base.ChunkType/StreamChunk）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class CommandType(Enum):
    """命令按执行模式分三类（恰三值，见 spec 能力 4）。

    LOCAL       纯本地：只读 / 只打印（查状态 / 看记忆），全程零 LLM。
    AFFECTS_UI  影响界面状态：改模式 / 清历史 / 压缩上下文，零 LLM。
    PROMPT      提示词类：把一段预设提示词回灌为本轮用户消息、交给 AI 跑一轮。
    """

    LOCAL = "local"
    AFFECTS_UI = "affects_ui"
    PROMPT = "prompt"


@dataclass
class CommandResult:
    """一条命令执行后的控制信号（回传主循环消费）。

    命令的可见输出由处理函数经 ``UIControl.show`` 自行打印；本结果只承载控制流：
      - ``exit``   ：请求退出主循环（/exit、/quit）。
      - ``prompt`` ：提示词类命令要回灌为本轮用户消息的预设提示词（非 None 即触发 AI 回合）。
    """

    exit: bool = False
    prompt: str | None = None


# 处理函数签名：只接「界面控制接口 + 参数原文」，返回控制信号（async 统一，便于 /compact 等 await）。
CommandHandler = Callable[["UIControl", str], Awaitable[CommandResult]]


@dataclass
class CommandSpec:
    """一条命令的全部元数据（单一来源：分流 / 补全 / 帮助都从此读）。"""

    name: str                       # 命令名（不含前导 /，小写）
    summary: str                    # 简短描述（/help 用）
    type: CommandType               # 命令类型（三类之一）
    handler: CommandHandler         # 处理函数
    aliases: tuple[str, ...] = ()   # 别名（不含 /）；可空
    usage: str = ""                 # 用法示例（可选）
    arg_hint: str = ""              # 参数提示（可选，如 "<路径>"）
    hidden: bool = False            # 隐藏命令：不进补全 / 帮助列表（如 /exit、/mode）


class UIControl(Protocol):
    """界面控制接口：抽象「渲染与应用控制」这一与框架绑定的部分。

    命令处理函数只依赖本协议取能力 / 领域数据，不直接依赖任何终端渲染 / 输入补全框架；
    TUI（或任何其它前端）实现本协议即可驱动全部命令。提示词类命令「提交用户消息触发
    AI 回合」经 ``CommandResult.prompt`` 回灌实现（主循环消费），不在本协议内重复造轮子。
    """

    # ── 渲染 ────────────────────────────────────────────────────────────────────
    def show(self, text: str) -> None:
        """打印一条消息到界面。"""
        ...

    def list_commands(self) -> list[CommandSpec]:
        """非隐藏命令列表（供 /help 由注册中心数据生成）。"""
        ...

    # ── 模式（#0007 权限模式：plan ↔ default 等）────────────────────────────────
    def get_mode(self) -> str:
        """读取当前权限模式。"""
        ...

    def set_mode(self, mode: str) -> None:
        """切换权限模式并即时反映到状态栏。"""
        ...

    def mode_command(self, args: str) -> None:
        """/mode 高级档视图 / 切换（保留既有路径：查看当前模式或切到 acceptEdits 等）。"""
        ...

    # ── 模型查看 / 切换（运行时即时生效）──────────────────────────────────────────
    def model_info(self) -> dict:
        """当前模型信息：``{"model": str, "protocol": str}``。"""
        ...

    def set_model(self, name: str) -> None:
        """切换本会话使用的模型（运行时即时生效）。"""
        ...

    # ── delegate（团队协调）切换（#0017 T6：触发入口从 Shift+Tab 移到 /delegate）──────────
    def toggle_delegate(self) -> bool | None:
        """切换 delegate（团队协调模式）：返回切换后的新状态（True/False）；
        团队功能未开启时返回 None（不接管，delegate 保持 False）。"""
        ...

    # ── token 用量（#0009）──────────────────────────────────────────────────────
    def token_stats(self) -> str | None:
        """实时 token 用量字符串（形如 ``12.3k / 200k``）；未启用上下文管理为 None。"""
        ...

    # ── 上下文压缩 / 历史（#0009 + 会话历史）─────────────────────────────────────
    def has_context(self) -> bool:
        """是否启用了上下文管理（决定 /compact 能否压缩）。"""
        ...

    async def compact(self) -> str:
        """手动压缩上下文，返回 ``before → after`` 统计串。"""
        ...

    def clear_history(self) -> None:
        """清空会话历史并落盘。"""
        ...

    # ── 只读领域数据（管理类命令用）──────────────────────────────────────────────
    def memory_index(self) -> str | None:
        """长期记忆索引文本（#0010）；无记忆 / 无 store 为 None。"""
        ...

    def session_info(self) -> dict:
        """会话信息：``{"count": 消息条数, "session_id": str | None}``。"""
        ...

    def permission_info(self) -> dict:
        """权限摘要：``{"mode", "deny", "ask", "allow", "local_path"}``（#0007）。"""
        ...

    # ── 技能系统（#0012）──────────────────────────────────────────────────────────
    def skills_overview(self) -> list[dict] | None:
        """已发现技能概览 ``[{"name","description","mode","active"}, ...]``；未启用技能系统为 None。"""
        ...

    def reload_skills(self) -> int | None:
        """手动重扫技能（热更新），返回有效技能数；未启用技能系统为 None。"""
        ...
