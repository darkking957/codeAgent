"""T2｜三级发现 + 同名覆盖 + 单文件/目录型识别 + 解析失败跳过。

三级目录（优先级 项目 > 用户 > 内置）：
  项目级  ./.coreagent/skills/
  用户级  ~/.coreagent/skills/
  内置级  coreagent/skills/builtins/

覆盖语义：同名技能高优先级整条替换低优先级。实现上按「内置 → 用户 → 项目」顺序扫描、后者
覆盖前者（dict 直接赋值），故项目级最终胜出。

形态识别：
  单文件型  目录内的 *.md 文件（SKILL.md 除外的散落 .md 也算单文件）。
  目录型    含入口 SKILL.md 的子目录；其 impl.py 专属工具经 dedtools 进程内加载、挂到 spec。

健壮性：单个技能解析 / 加载失败只记 warning 并跳过该项，不抛、不阻断其余（spec 非功能要求）。
"""

import logging
from pathlib import Path

from coreagent.skills import constants
from coreagent.skills.dedtools import load_dedicated_tools
from coreagent.skills.parser import parse_skill
from coreagent.skills.types import SkillParseError, SkillSpec

logger = logging.getLogger(__name__)


def _level_dirs(project_dir: Path | str | None) -> list[Path]:
    """按「内置 → 用户 → 项目」返回三级目录（低优先级在前，便于后者覆盖前者）。"""
    dirs = [constants.BUILTIN_SKILLS_DIR, constants.USER_SKILLS_DIR]
    if project_dir is not None:
        dirs.append(Path(project_dir) / constants.PROJECT_SKILLS_RELDIR)
    return dirs


def _load_single_file(path: Path) -> SkillSpec | None:
    """解析一个单文件型技能；失败记 warning 返回 None。"""
    try:
        spec = parse_skill(path.read_text(encoding="utf-8"))
    except (SkillParseError, OSError, UnicodeDecodeError) as e:
        logger.warning("技能文件解析失败，已跳过：%s：%s", path, e)
        return None
    return spec


def _load_directory(skill_dir: Path) -> SkillSpec | None:
    """解析一个目录型技能（入口 SKILL.md + 专属工具 impl.py）；失败记 warning 返回 None。"""
    entry = skill_dir / constants.SKILL_ENTRY_FILENAME
    try:
        spec = parse_skill(entry.read_text(encoding="utf-8"))
    except (SkillParseError, OSError, UnicodeDecodeError) as e:
        logger.warning("目录型技能解析失败，已跳过：%s：%s", skill_dir, e)
        return None
    spec.dir_path = skill_dir
    # 专属工具：进程内加载（失败软化，返回空列表）；激活时注册，启动扫描即已知其名（供白名单校验）。
    spec.dedicated_tools = load_dedicated_tools(skill_dir)
    return spec


def _scan_dir(level_dir: Path, out: dict[str, SkillSpec]) -> None:
    """扫描单个层级目录，把发现的技能写入 out（同名覆盖：后扫描者胜）。"""
    if not level_dir.exists() or not level_dir.is_dir():
        return
    for entry in sorted(level_dir.iterdir()):
        spec: SkillSpec | None = None
        if entry.is_dir():
            if (entry / constants.SKILL_ENTRY_FILENAME).is_file():
                spec = _load_directory(entry)
        elif entry.is_file() and entry.suffix == constants.SKILL_FILE_SUFFIX:
            spec = _load_single_file(entry)
        if spec is not None:
            out[spec.name] = spec  # 同名覆盖（项目级最后扫描，故胜出）


def discover_skills(project_dir: Path | str | None = None) -> dict[str, SkillSpec]:
    """扫描三级目录，返回「技能名 → SkillSpec」映射（项目 > 用户 > 内置，同名覆盖）。

    单个技能失败只跳过该项；整体绝不抛异常（发现层对装配无害）。
    """
    out: dict[str, SkillSpec] = {}
    for level_dir in _level_dirs(project_dir):
        try:
            _scan_dir(level_dir, out)
        except OSError as e:
            logger.warning("技能目录扫描失败，已跳过该层：%s：%s", level_dir, e)
    return out
