"""T1｜技能文件解析：YAML frontmatter + Markdown 正文 + 占位符整体替换。

文件格式（单文件型 *.md 与目录型 SKILL.md 同构）：

    ---
    name: commit
    description: 生成规范提交信息并提交
    allowed-tools: [run_command, read_file]
    mode: shared
    max-history-tokens: 4000
    model: claude-x
    ---
    <正文 SOP，可含 $ARGUMENTS 占位符>

纪律：
- 缺 frontmatter / 缺 name / 缺 description / mode 非法 / frontmatter 非映射 → 抛 SkillParseError；
- allowed-tools 须为列表（缺省空列表）；max-history-tokens 须为正整数（缺省默认值）；
- 占位符替换走 str.replace（整体替换，不引模板引擎——最简方案优先）。
"""

import yaml

from coreagent.skills import constants
from coreagent.skills.types import SkillMode, SkillParseError, SkillSpec

# frontmatter 分隔线（首行须为此）。
_FENCE = "---"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """切出 (frontmatter 映射, 正文)；无合法 frontmatter 抛 SkillParseError。"""
    lines = text.splitlines()
    # 跳过文件开头的空行后，首个有效行必须是分隔线。
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != _FENCE:
        raise SkillParseError("技能文件缺少 YAML frontmatter（须以 --- 起始）")
    # 找闭合分隔线。
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break
    if end is None:
        raise SkillParseError("技能 frontmatter 未闭合（缺少结束的 ---）")
    fm_text = "\n".join(lines[start + 1 : end])
    body = "\n".join(lines[end + 1 :])
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise SkillParseError(f"技能 frontmatter YAML 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise SkillParseError("技能 frontmatter 必须为映射（key: value 形式）")
    return data, body.strip("\n")


def _parse_mode(value) -> SkillMode:
    """mode 字段 → SkillMode；缺省 shared；非法值抛 SkillParseError（供跳过）。"""
    if value is None:
        return SkillMode.SHARED
    text = str(value).strip()
    try:
        return SkillMode(text)
    except ValueError:
        raise SkillParseError(
            f"技能 mode 取值非法：{value!r}（仅允许 "
            f"{constants.MODE_SHARED} / {constants.MODE_INDEPENDENT}）"
        ) from None


def _parse_allowed_tools(value) -> list[str]:
    """allowed-tools 字段 → 工具名列表；缺省空列表；非列表抛 SkillParseError。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SkillParseError(
            f"技能 {constants.FIELD_ALLOWED_TOOLS} 必须为列表：当前为 {type(value).__name__}"
        )
    return [str(v).strip() for v in value if str(v).strip()]


def _parse_max_history_tokens(value) -> int:
    """max-history-tokens 字段 → 正整数；缺省默认值；非正整数抛 SkillParseError。"""
    if value is None:
        return constants.DEFAULT_MAX_HISTORY_TOKENS
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise SkillParseError(
            f"技能 {constants.FIELD_MAX_HISTORY_TOKENS} 必须为整数：当前为 {value!r}"
        ) from None
    if n <= 0:
        raise SkillParseError(
            f"技能 {constants.FIELD_MAX_HISTORY_TOKENS} 必须为正整数：当前为 {n}"
        )
    return n


def parse_skill(text: str) -> SkillSpec:
    """把一个技能文件文本解析为 SkillSpec；任何不合法处抛 SkillParseError。

    必填：name、description。其余字段缺省走默认值。dir_path / dedicated_tools 由发现层补充。
    """
    data, body = _split_frontmatter(text)

    name = str(data.get(constants.FIELD_NAME, "")).strip()
    if not name:
        raise SkillParseError("技能缺少必填字段 name")
    description = str(data.get(constants.FIELD_DESCRIPTION, "")).strip()
    if not description:
        raise SkillParseError(f"技能 {name} 缺少必填字段 description")

    model_raw = data.get(constants.FIELD_MODEL)
    model = str(model_raw).strip() if model_raw is not None and str(model_raw).strip() else None

    return SkillSpec(
        name=name,
        description=description,
        body=body,
        mode=_parse_mode(data.get(constants.FIELD_MODE)),
        allowed_tools=_parse_allowed_tools(data.get(constants.FIELD_ALLOWED_TOOLS)),
        max_history_tokens=_parse_max_history_tokens(data.get(constants.FIELD_MAX_HISTORY_TOKENS)),
        model=model,
    )


def render_body(body: str, arguments: str | None) -> str:
    """把正文里的 $ARGUMENTS 占位符整体替换为传入参数（None / 空 → 替换为空串）。"""
    return body.replace(constants.ARGUMENTS_PLACEHOLDER, arguments or "")
