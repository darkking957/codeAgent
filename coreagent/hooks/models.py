"""Hook 规则数据模型 + 全部固定值常量（#0013 T1）。

三要素规则：``event``（触发时刻，必填）+ ``if``（条件，可省）+ ``action``（动作，必填），
外加可选执行控制（only-once / async / timeout）。本模块只放纯数据与常量，不含加载 / 求值
逻辑（loader.py / conditions.py / actions.py 分担），故无对外依赖（被它们共同 import）。

固定值（事件名 / 动作类型 / 退出码 / 阈值）以 specs/0013-hooks/checklist.md 的固定值表为准。
"""

from dataclasses import dataclass, field

from coreagent.errors import ConfigError

# ── 生命周期事件名（10 个精确字符串，见 checklist 固定值表）───────────────────────
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
STOP = "Stop"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
POST_TOOL_USE_FAILURE = "PostToolUseFailure"
PERMISSION_DENIED = "PermissionDenied"
PRE_COMPACT = "PreCompact"
POST_COMPACT = "PostCompact"

EVENTS = frozenset({
    SESSION_START, SESSION_END, USER_PROMPT_SUBMIT, STOP,
    PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_USE_FAILURE, PERMISSION_DENIED,
    PRE_COMPACT, POST_COMPACT,
})

# 携带工具信息（tool_name/tool_input）、可对其求值 `if` 的事件集；其余事件带 `if` → 静默跳过。
TOOL_EVENTS = frozenset({
    PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_USE_FAILURE, PERMISSION_DENIED,
})

# 同步决策型（可拦截 / 可阻断、必须同步等待）事件集；其余为异步观测型。拦截类禁止 async。
BLOCKING_EVENTS = frozenset({PRE_TOOL_USE, PRE_COMPACT})

# ── 动作类型白名单（见 checklist 固定值表）──────────────────────────────────────
ACTION_COMMAND = "command"
ACTION_PROMPT = "prompt"
ACTION_HTTP = "http"
ACTION_SUBAGENT = "subagent"
ACTION_TYPES = frozenset({ACTION_COMMAND, ACTION_PROMPT, ACTION_HTTP, ACTION_SUBAGENT})

# ── 退出码语义（命令动作）──────────────────────────────────────────────────────
EXIT_CONTINUE = 0   # 继续 / 放行
EXIT_BLOCK = 2      # 硬拦（仅 PreToolUse / PreCompact 生效）

# ── 阈值 / 默认值 ──────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = 60        # 命令动作默认超时（秒）
INJECT_MAX_CHARS = 10000    # 注入文本上限（超限存文件、正文替换为预览 + 路径）

# 反向标记前缀：规则字符串前缀 `!` 表示「命中 = 调用不匹配该规则」。
NEGATE_PREFIX = "!"


class HookConfigError(ConfigError):
    """Hook 规则 / 配置非法（事件名 / 动作类型 / 控制组合 / 条件结构错误）。"""


@dataclass
class Action:
    """一条动作：类型 + 该类型的字段。未用到的字段留默认。"""

    type: str
    command: str | None = None          # command：shell 命令
    prompt: str | None = None           # prompt：注入正文
    url: str | None = None              # http：请求地址
    method: str = "POST"                # http：请求方法
    subagent: str | None = None         # subagent：占位（子 Agent 描述 / 名称）


@dataclass
class Hook:
    """一条编译后的 Hook 规则。``condition`` 由 conditions.compile_condition 产出（可为 None）。"""

    event: str
    action: Action
    condition: object | None = None     # conditions.Condition | None（无 `if` 时 None）
    only_once: bool = False
    async_: bool = False
    timeout: int = DEFAULT_TIMEOUT
    source: str = ""                    # 来源档（project / user），仅日志用
    raw: dict = field(default_factory=dict)  # 原始 YAML 片段，仅日志用


def is_tool_event(event: str) -> bool:
    """该事件是否携带工具信息（可对其求值 `if`）。"""
    return event in TOOL_EVENTS


def is_blocking_event(event: str) -> bool:
    """该事件是否为同步决策型（可拦截 / 可阻断）。"""
    return event in BLOCKING_EVENTS
