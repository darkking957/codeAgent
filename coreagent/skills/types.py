"""T1｜技能契约层：执行模式枚举 + SkillSpec 数据结构 + 解析异常。

SkillSpec 承载一个技能的全部信息：frontmatter 全字段（名字 / 说明 / 白名单 / 执行模式 /
带入历史预算 / 可选模型）+ 正文 SOP；目录型技能另带 `dir_path` 与发现期加载好的
`dedicated_tools`（专属 Tool 实例）。解析失败统一抛 SkillParseError，供发现层捕获后跳过。

Enum + dataclass 范式对齐既有模块（permissions.rules、commands.types、agent.ConfirmDecision）。
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from coreagent.skills import constants


class SkillMode(Enum):
    """技能执行模式（恰两值，见 checklist 契约）。

    SHARED      在当前对话内执行，指令钉入上下文、激活态持续到清空对话。
    INDEPENDENT 开独立子对话执行，跑完由模型自产收尾摘要回流主历史，不长期钉激活。
    """

    SHARED = constants.MODE_SHARED
    INDEPENDENT = constants.MODE_INDEPENDENT


class SkillParseError(Exception):
    """技能解析错误（缺 frontmatter / 缺必填字段 / mode 非法等）；供发现层捕获后跳过该技能。"""


@dataclass
class SkillSpec:
    """一个技能的全部元数据 + 正文 SOP（单一来源：发现 / 渲染 / 激活 / 命令都从此读）。"""

    name: str                                   # 技能名（命令分发 / 覆盖 / 激活的键）
    description: str                            # 一句话说明（启动目录块用）
    body: str                                   # 正文 SOP（发给模型的指令，含 $ARGUMENTS 占位）
    mode: SkillMode = SkillMode.SHARED          # 执行模式
    allowed_tools: list[str] = field(default_factory=list)   # 可见工具白名单（裁剪用）
    max_history_tokens: int = constants.DEFAULT_MAX_HISTORY_TOKENS  # 独立模式带入历史预算
    model: str | None = None                    # 可选专属模型（仅独立模式生效）
    # ── 发现层补充（解析层不填）─────────────────────────────────────────────────
    dir_path: Path | None = None                # 目录型技能的目录；单文件型为 None
    dedicated_tools: list = field(default_factory=list)  # 发现期加载好的专属 Tool 实例

    @property
    def is_directory(self) -> bool:
        return self.dir_path is not None
