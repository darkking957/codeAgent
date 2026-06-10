"""T1｜角色契约层：AgentRole 数据结构 + 解析异常。

AgentRole 承载一个子 Agent 角色的全部信息：frontmatter 全字段（名字 / 说明 / 工具白名单 /
工具黑名单 / 可选模型 / 最大轮次 / 权限模式）+ 正文系统提示。解析失败统一抛 AgentParseError，
供发现层捕获后跳过（仿 #0012 SkillSpec / SkillParseError）。

模型字段字符串透传（同 #0012 技能）：缺省 None 即「继承父模型」。权限模式缺省 dontAsk
（子 Agent 默认非交互模式，见 #0007 modes.DONT_ASK）。
"""

from dataclasses import dataclass, field
from pathlib import Path

from coreagent.permissions.modes import DONT_ASK


@dataclass
class AgentRole:
    """一个子 Agent 角色的全部元数据 + 系统提示正文（单一来源：发现 / 运行都从此读）。"""

    name: str                                   # 角色名（委派 / 覆盖的键）
    description: str                            # 一句话用途说明
    body: str                                   # 正文 = 子 Agent 的系统提示（定义身份 / 职责 / 风格）
    allowed_tools: list[str] = field(default_factory=list)   # 工具白名单（空 = 不额外收窄）
    denied_tools: list[str] = field(default_factory=list)    # 工具黑名单（从可见集剔除）
    model: str | None = None                    # 可选模型（字符串透传；缺省 None = 继承父模型）
    max_turns: int | None = None                # 最大轮次（缺省 None = 用 Agent Loop 默认上限）
    permission_mode: str = DONT_ASK             # 权限模式（缺省 dontAsk，子 Agent 非交互默认）
    isolation: str | None = None                # 隔离模式（#0015；仅 "worktree" 生效，缺省 None = 无隔离）
    # ── 发现层补充（解析层不填）─────────────────────────────────────────────────
    source: str | None = None                   # 来源层（project/user/builtin/plugin），仅诊断用
    dir_path: Path | None = None                # 角色文件所在目录（诊断用）


class AgentParseError(Exception):
    """角色解析错误（缺 frontmatter / 缺必填字段 / 字段类型非法等）；供发现层捕获后跳过该角色。"""
