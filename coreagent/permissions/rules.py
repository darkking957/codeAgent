"""规则数据模型 + 匹配语法（#0007 T1）。

规则语法：``Alias(pattern)``，例如 ``Bash(git diff *)``、``Read(./.env)``；裸 ``Alias``
或空 pattern 视作「宽泛」（匹配该工具全部调用）。别名是 Claude Code 式的工具名
（Bash/Read/Write/Edit/Glob/Grep），内部维护别名 ↔ 内部工具名映射。

匹配分两类目标：
  - 命令类（run_command）：对命令字符串做 glob / 前缀匹配。末尾 ``空格 *`` 视作前缀匹配
    （前缀本身或「前缀+空格+任意」均命中），故 ``Bash(git diff *)`` 命中 ``git diff HEAD``
    但不命中 ``git difftool``。
  - 路径类（read/write/edit/glob/grep）：把 pattern 与工具路径参数都解析为绝对路径后 fnmatch，
    故 ``Read(./.env)`` 命中解析后落在 ``<cwd>/.env`` 的读，不命中 ``./envrc``。

未知别名、非法语法判为配置错误（PermissionConfigError，fail-closed 上层据此报中文错）。
"""

import os
import re
from dataclasses import dataclass

from coreagent.errors import ConfigError

# ── 裁决结果（Decision.outcome）────────────────────────────────────────────────
DENY = "deny"
ASK = "ask"
ALLOW = "allow"

# ── 作用域标签（Rule.scope；scopes.py / defaults.py 赋值）──────────────────────
SCOPE_BUILTIN = "builtin"   # 随包预置白名单（最低优先、升档不丢弃）

# ── 别名 ↔ 内部工具名（见 checklist 固定值表）─────────────────────────────────
ALIAS_TO_TOOL = {
    "Bash": "run_command",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Glob": "glob",
    "Grep": "grep",
}
TOOL_TO_ALIAS = {v: k for k, v in ALIAS_TO_TOOL.items()}

# 命令类工具（匹配命令字符串）；其余按路径匹配。
_COMMAND_TOOLS = {"run_command"}
# 路径参数所在的入参键：read/write/edit 用 path；glob/grep 用 path（搜索根，缺省 "."）。
_PATH_KEY = "path"

# 规则语法：别名，或别名(pattern)。pattern 内可含任意字符（含右括号外的内容由贪婪到末括号截取）。
_RULE_RE = re.compile(r"^([A-Za-z]+)(?:\((.*)\))?$")


class PermissionConfigError(ConfigError):
    """权限规则 / 配置非法（语法错误、未知别名等）。"""


@dataclass
class Decision:
    """一次授权裁决（流水线 / 引擎产出，审计可消费）。"""

    outcome: str                      # deny / ask / allow
    stage: str = "rules"              # prefilter / rules / classifier / bypass / mode:* / default
    matched_rule: str | None = None   # 命中的规则原文（未命中为 None）
    scope: str | None = None          # 命中规则的作用域（managed/project/user/local/builtin/None）
    reason: str = ""                  # 人类可读理由（审计 / 回灌模型用）


@dataclass
class Rule:
    """一条编译后的规则：别名 + 内部工具名 + pattern + 来源作用域。"""

    raw: str            # 规则原文，如 "Bash(git diff *)"
    alias: str          # "Bash"
    tool_name: str      # "run_command"
    pattern: str        # "git diff *"（空串表示宽泛）
    scope: str | None = None

    @property
    def is_broad(self) -> bool:
        """宽泛规则：pattern 为 `*`（裸通配整个参数）或空（见 checklist 升档判定基准）。"""
        return self.pattern in ("", "*")

    def matches(self, tool_name: str, tool_input: dict) -> bool:
        """该规则是否命中给定工具调用。"""
        if tool_name != self.tool_name:
            return False
        if self.is_broad:
            return True
        target = _match_target(tool_name, tool_input)
        if tool_name in _COMMAND_TOOLS:
            return _match_command(target, self.pattern)
        return _match_path(target, self.pattern)


def _match_target(tool_name: str, tool_input: dict) -> str:
    """取该工具的匹配目标：命令类取 command；路径类取 path（glob/grep 缺省 "."）。"""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name in _COMMAND_TOOLS:
        return str(tool_input.get("command") or "")
    raw = tool_input.get(_PATH_KEY)
    if raw:
        return str(raw)
    # glob/grep 的搜索根缺省为当前目录；read/write/edit 缺 path 则空串（不会命中精确规则）。
    return "." if tool_name in ("glob", "grep") else ""


# shell 顶层分隔符：|| && ; & | 换行。
_SHELL_SEP = re.compile(r"\|\||&&|[;&|\n]")


def split_command(command: str) -> list[str]:
    """把 shell 命令按顶层分隔符（``;`` ``&&`` ``||`` ``|`` ``&`` 换行）粗分为子命令列表。

    不做完整 shell 解析：对引号内的分隔符会过度切分——这对授权是**安全方向**（更多子命令需
    各自被 allow 覆盖，倾向 ask；deny 仍逐段命中）。无分隔符时返回 ``[整条命令]``。
    """
    parts = [p.strip() for p in _SHELL_SEP.split(command)]
    parts = [p for p in parts if p]
    return parts or [command.strip()]


def _glob_match(text: str, pattern: str) -> bool:
    """只把 ``*`` 当通配（匹配任意串，含空），其余字符（含 ``?`` ``[`` ``]``）按**字面**匹配。

    规避 fnmatch 把 ``?`` / ``[]`` 当元字符的陷阱：deny 规则里的 ``[abc]`` 不会被解释成字符集而
    漏匹配（fail-open），persist 落盘的字面命令 / 路径也能原样再匹配（persist 往返不破）。
    """
    regex = ".*".join(re.escape(seg) for seg in pattern.split("*"))
    return re.match(f"^{regex}$", text) is not None


def _match_command(command: str, pattern: str) -> bool:
    """命令匹配：末尾 `空格 *` → 前缀匹配；其余只把 ``*`` 当通配、其余字面（见 _glob_match）。"""
    command = command.strip()
    pattern = pattern.strip()
    if pattern in ("", "*"):
        return True
    if pattern.endswith(" *"):
        prefix = pattern[:-2].strip()
        return command == prefix or command.startswith(prefix + " ")
    return _glob_match(command, pattern)


def _match_path(path: str, pattern: str) -> bool:
    """路径匹配：pattern 与路径同解析为绝对路径后比较。

    含 ``*`` 的 pattern 走 glob（只 ``*`` 通配）；**字面** pattern 走「精确命中或目录前缀」——
    使 deny 一个目录（如 ``Read(./secret)``）能保护其下全部内容，而非只匹配目录节点本身。
    """
    if pattern in ("", "*"):
        return True
    abs_path = os.path.abspath(os.path.expanduser(str(path)))
    abs_pat = os.path.abspath(os.path.expanduser(pattern))
    if "*" in pattern:
        return _glob_match(abs_path, abs_pat)
    return abs_path == abs_pat or abs_path.startswith(abs_pat + os.sep)


def parse_rule(raw: str, *, scope: str | None = None) -> Rule:
    """解析一条规则原文为 Rule；语法非法 / 未知别名抛 PermissionConfigError（中文）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise PermissionConfigError(f"权限规则非法：{raw!r}（应为非空字符串，形如 Alias(pattern)）")
    s = raw.strip()
    m = _RULE_RE.match(s)
    if not m:
        raise PermissionConfigError(
            f"权限规则语法非法：{raw!r}（应形如 Alias(pattern)，如 Bash(git diff *)）"
        )
    alias, pattern = m.group(1), m.group(2)
    if alias not in ALIAS_TO_TOOL:
        raise PermissionConfigError(
            f"未知工具别名：{alias!r}（出现在规则 {raw!r}）；"
            f"合法别名：{'/'.join(ALIAS_TO_TOOL)}"
        )
    return Rule(
        raw=s,
        alias=alias,
        tool_name=ALIAS_TO_TOOL[alias],
        pattern=(pattern or "").strip(),
        scope=scope,
    )


def rule_for_tool_call(tool_name: str, tool_input: dict) -> str:
    """据一次工具调用生成一条「精确 allow 规则」原文（供 persist 落盘 / 展示）。

    目标为空 / 裸通配时拒绝生成：否则会落成 ``Bash()`` / ``Write()`` 这类宽泛规则，
    永久放行该工具的**全部**调用（权限逃逸）。
    """
    alias = TOOL_TO_ALIAS.get(tool_name, tool_name)
    target = _match_target(tool_name, tool_input).strip()
    if not target or target == "*":
        raise PermissionConfigError(
            f"无法为 {tool_name} 生成持久化规则：目标为空（会变成放行该工具全部调用的宽泛规则）"
        )
    return f"{alias}({target})"
