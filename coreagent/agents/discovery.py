"""T2｜角色多来源发现 + 同名覆盖 + 解析失败跳过（仿 #0012 skills.discovery）。

四级目录（优先级 项目 > 用户 > 内置 > 插件）：
  项目级  ./.coreagent/agents/
  用户级  ~/.coreagent/agents/
  内置级  coreagent/agents/builtins/
  插件级  预留槽位（本仓无插件子系统，当前**不扫描任何插件目录**，待插件系统落地再接）。

覆盖语义：同名角色高优先级整条替换低优先级。实现上按「插件 → 内置 → 用户 → 项目」顺序扫描、
后者覆盖前者（dict 直接赋值），故项目级最终胜出。

健壮性：单个角色解析失败只记 warning 并跳过该项，不抛、不阻断其余（spec 非功能要求）。
"""

import logging
from pathlib import Path

from coreagent.agents import constants
from coreagent.agents.parser import parse_role
from coreagent.agents.types import AgentParseError, AgentRole

logger = logging.getLogger(__name__)


def _level_dirs(project_dir: Path | str | None) -> list[tuple[Path, str]]:
    """按「插件 → 内置 → 用户 → 项目」返回各级 (目录, 来源标签)（低优先级在前，便于后者覆盖前者）。

    插件级预留：本仓无插件子系统（已 grep 确认），PLUGIN_AGENTS_DIRS 当前为空 → 不 stat 任何插件
    目录。待插件系统落地后填充其角色目录即自动接入（TODO 见 constants.PLUGIN_AGENTS_DIRS）。
    """
    dirs: list[tuple[Path, str]] = [(d, "plugin") for d in constants.PLUGIN_AGENTS_DIRS]
    dirs.append((constants.BUILTIN_AGENTS_DIR, "builtin"))
    dirs.append((constants.USER_AGENTS_DIR, "user"))
    if project_dir is not None:
        dirs.append((Path(project_dir) / constants.PROJECT_AGENTS_RELDIR, "project"))
    return dirs


def _load_single_file(path: Path, source: str) -> AgentRole | None:
    """解析一个单文件型角色；失败记 warning 返回 None。"""
    try:
        role = parse_role(path.read_text(encoding="utf-8"))
    except (AgentParseError, OSError, UnicodeDecodeError) as e:
        logger.warning("角色文件解析失败，已跳过：%s：%s", path, e)
        return None
    role.source = source
    role.dir_path = path.parent
    return role


def _scan_dir(level_dir: Path, source: str, out: dict[str, AgentRole]) -> None:
    """扫描单个层级目录，把发现的角色写入 out（同名覆盖：后扫描者胜）。"""
    if not level_dir.exists() or not level_dir.is_dir():
        return
    for entry in sorted(level_dir.iterdir()):
        if entry.is_file() and entry.suffix == constants.AGENT_FILE_SUFFIX:
            role = _load_single_file(entry, source)
            if role is not None:
                out[role.name] = role  # 同名覆盖（项目级最后扫描，故胜出）


def discover_roles(project_dir: Path | str | None = None) -> dict[str, AgentRole]:
    """扫描四级目录，返回「角色名 → AgentRole」映射（项目 > 用户 > 内置 > 插件，同名覆盖）。

    单个角色失败只跳过该项；整体绝不抛异常（发现层对装配无害）。
    """
    out: dict[str, AgentRole] = {}
    for level_dir, source in _level_dirs(project_dir):
        try:
            _scan_dir(level_dir, source, out)
        except OSError as e:
            logger.warning("角色目录扫描失败，已跳过该层：%s：%s", level_dir, e)
    return out
