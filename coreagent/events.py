"""引擎级类型化事件（#0018）。

``run_agent_turn`` 把多轮循环过程翻译为这组**单向**事件 yield 给消费者：CLI 据此渲染、
headless 消费者（子 Agent / 团队队员 / 独立技能）据此记账。事件词汇与 provider 的
``StreamChunk`` 解耦——引擎循环信号（轮次 / 终止 / 错误 / 重试）归这里，provider 枚举只保留
原生流式类型。

纯数据类型（dataclass + enum），无行为、无 I/O：**绝不** import 渲染 / 终端 / provider 模块；
仅依赖标准库与本仓纯数据类型 ``ToolResult``（工具结果事件需承载其 content / success）。
控制型 / 双向交互（confirm 人工确认、gate 门禁、plan 评审）表达不了单向 yield，仍是引擎参数，
不在本事件集内（留后续双向协议统一设计）。
"""

import enum
from dataclasses import dataclass

from coreagent.tools.base import ToolResult


class TextKind(enum.Enum):
    """流式文本通道：思考通道（thinking）/ 回答通道（answer）。"""

    THINKING = "thinking"
    ANSWER = "answer"


class RunEndReason(enum.Enum):
    """运行级终止原因：自然完成 / 达轮数上限 / 被取消。"""

    COMPLETE = "complete"      # 模型本轮无工具调用，自然完成
    MAX_TURNS = "max_turns"    # 达到轮数上限
    CANCELLED = "cancelled"    # 收到取消信号


@dataclass
class TextDelta:
    """流式文本增量；``kind`` 区分思考通道与回答通道。"""

    kind: TextKind
    text: str


@dataclass
class ToolCall:
    """一次工具调用发起（按模型**调用序** emit）。``call`` = {"id","name","input"}。"""

    call: dict


@dataclass
class ToolResultEvent:
    """一次工具调用的结果（读类**完成序** / 写类串行序 emit）；含失败与被拒。

    - ``result``：本仓 ``ToolResult``（content + success）；
    - ``rejected``：True 表示该调用被拒（门禁 deny / 用户拒绝 / Hook 拦截），非真实执行产出；
    - ``is_error``：渲染 / 记账用的失败标记（被拒或执行失败均为 True）。
    """

    call: dict
    result: ToolResult
    rejected: bool = False

    @property
    def is_error(self) -> bool:
        return self.rejected or not self.result.success


@dataclass
class TurnStart:
    """新一轮模型调用开始（仅带轮号，从 1 起）。"""

    round_index: int


@dataclass
class Retry:
    """模型调用瞬时失败后的重试提示：第 ``attempt`` 次重试将在 ``wait`` 秒后发起。

    在退避 sleep **之前** emit（保留「先提示 → 再等待 → 后重发」时序）。
    """

    attempt: int
    wait: int


@dataclass
class RunDone:
    """运行级终止事件：携带终止原因、plan、**累计** token 用量。

    - ``reason``：``RunEndReason``（complete / max_turns / cancelled）；
    - ``plan``：plan 模式下被拦截写类的计划项列表（非 plan 模式为 None）；
    - ``input_tokens`` / ``output_tokens``：本次运行**累计**服务端 token 用量（各轮之和）。
    """

    reason: RunEndReason
    plan: list | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RunError:
    """运行级不可恢复错误事件：携带异常类型名（区别于单个工具失败的结构化结果）。"""

    error_type: str
