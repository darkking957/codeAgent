"""T1｜角色文件解析：YAML frontmatter + Markdown 正文（系统提示）。

文件格式（单文件型 *.md）：

    ---
    name: code-reviewer
    description: 资深代码审查员，只报影响正确性的问题
    allowed-tools: [read_file, grep, glob, run_command]
    denied-tools: [write_file, edit_file]
    model: claude-x
    max-turns: 12
    permission-mode: dontAsk
    ---
    <正文 = 子 Agent 的系统提示，定义身份 / 职责 / 工作风格>

纪律（仿 #0012 skills.parser）：
- 缺 frontmatter / 缺 name / 缺 description / frontmatter 非映射 → 抛 AgentParseError；
- allowed-tools / denied-tools 须为列表（缺省空列表）；max-turns 须为正整数（缺省 None=继承默认）；
- model 字符串透传（缺省 None）；permission-mode 经 normalize_mode 规整（非法回落 default，缺省
  dontAsk）。
"""

import yaml

from coreagent.agents import constants
from coreagent.agents.types import AgentParseError, AgentRole
from coreagent.permissions.modes import DONT_ASK, normalize_mode

# frontmatter 分隔线（首个有效行须为此）。
_FENCE = "---"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """切出 (frontmatter 映射, 正文)；无合法 frontmatter 抛 AgentParseError。"""
    lines = text.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != _FENCE:
        raise AgentParseError("角色文件缺少 YAML frontmatter（须以 --- 起始）")
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break
    if end is None:
        raise AgentParseError("角色 frontmatter 未闭合（缺少结束的 ---）")
    fm_text = "\n".join(lines[start + 1 : end])
    body = "\n".join(lines[end + 1 :])
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise AgentParseError(f"角色 frontmatter YAML 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise AgentParseError("角色 frontmatter 必须为映射（key: value 形式）")
    return data, body.strip("\n")


def _parse_tool_list(value, field_name: str) -> list[str]:
    """工具名列表字段（allowed-tools / denied-tools）→ 列表；缺省空列表；非列表抛 AgentParseError。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise AgentParseError(
            f"角色 {field_name} 必须为列表：当前为 {type(value).__name__}"
        )
    return [str(v).strip() for v in value if str(v).strip()]


def _parse_max_turns(value) -> int | None:
    """max-turns 字段 → 正整数；缺省 None（继承 Agent Loop 默认上限）；非正整数抛 AgentParseError。"""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise AgentParseError(
            f"角色 {constants.FIELD_MAX_TURNS} 必须为整数：当前为 {value!r}"
        ) from None
    if n <= 0:
        raise AgentParseError(
            f"角色 {constants.FIELD_MAX_TURNS} 必须为正整数：当前为 {n}"
        )
    return n


def _parse_permission_mode(value) -> str:
    """permission-mode 字段 → 规整后的模式名；缺省 dontAsk（子 Agent 默认）；非法经 normalize 回落。"""
    if value is None:
        return DONT_ASK
    return normalize_mode(str(value).strip())


def _parse_isolation(value) -> str | None:
    """isolation 字段 → 隔离模式（#0015）；仅认 worktree（大小写不敏感），其余 / 缺省 → None（无隔离）。"""
    if value is None:
        return None
    return (
        constants.ISOLATION_WORKTREE
        if str(value).strip().lower() == constants.ISOLATION_WORKTREE
        else None
    )


def parse_role(text: str) -> AgentRole:
    """把一个角色文件文本解析为 AgentRole；任何不合法处抛 AgentParseError。

    必填：name、description。其余字段缺省走默认值。source / dir_path 由发现层补充。
    """
    data, body = _split_frontmatter(text)

    name = str(data.get(constants.FIELD_NAME, "")).strip()
    if not name:
        raise AgentParseError("角色缺少必填字段 name")
    description = str(data.get(constants.FIELD_DESCRIPTION, "")).strip()
    if not description:
        raise AgentParseError(f"角色 {name} 缺少必填字段 description")

    model_raw = data.get(constants.FIELD_MODEL)
    model = str(model_raw).strip() if model_raw is not None and str(model_raw).strip() else None

    return AgentRole(
        name=name,
        description=description,
        body=body,
        allowed_tools=_parse_tool_list(data.get(constants.FIELD_ALLOWED_TOOLS), constants.FIELD_ALLOWED_TOOLS),
        denied_tools=_parse_tool_list(data.get(constants.FIELD_DENIED_TOOLS), constants.FIELD_DENIED_TOOLS),
        model=model,
        max_turns=_parse_max_turns(data.get(constants.FIELD_MAX_TURNS)),
        permission_mode=_parse_permission_mode(data.get(constants.FIELD_PERMISSION_MODE)),
        isolation=_parse_isolation(data.get(constants.FIELD_ISOLATION)),
    )
