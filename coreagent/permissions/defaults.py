"""预置默认规则 / 白名单模板（#0007 T7）。

随包分发的只读基线：放行读类工具（Read/Glob/Grep）与常见安全只读命令，使**默认配置下读
零摩擦**；fail-closed 只对未覆盖的写 / 命令 / 敏感读生效。预置规则属 builtin 作用域，
优先级最低（可被任何高作用域 deny 覆盖），且升档时不被丢弃（见 modes.is_escalated）。
"""

from coreagent.permissions.rules import SCOPE_BUILTIN, Rule, parse_rule

# 预置 allow 清单（见 checklist 固定值）：广读 + 常见只读命令。
PRESET_ALLOW = [
    "Read(*)",
    "Glob(*)",
    "Grep(*)",
    "Bash(git status *)",
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(ls *)",
    "Bash(cat *)",
    "Bash(pwd)",
]


def builtin_allow_rules() -> list[Rule]:
    """编译预置 allow 清单为 Rule 列表（scope=builtin）。"""
    return [parse_rule(s, scope=SCOPE_BUILTIN) for s in PRESET_ALLOW]
