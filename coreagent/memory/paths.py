"""T2｜持久化基建：项目哈希 / 会话 ID / 目录布局 / 过期清理。

布局：<root>/projects/<project-hash>/<session-id>/session-memory/summary.md
project-hash = 项目绝对路径 sha256 十六进制前 16 位（同项目稳定、不同项目互异）。
session-id   = YYYYMMDD-HHMMSS-xxxx（4 位随机十六进制后缀防同秒撞车）。
清理         = 按 session-id 内嵌时间戳判龄，超 retention_days 的目录整棵删除。
"""

import hashlib
import logging
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from coreagent.memory import constants

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(constants.SESSION_ID_REGEX)


def project_hash(project_dir: Path | str) -> str:
    """项目绝对路径的稳定哈希（sha256 十六进制前 16 位）。"""
    abs_path = str(Path(project_dir).resolve())
    digest = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()
    return digest[: constants.PROJECT_HASH_LEN]


def new_session_id(now: datetime | None = None) -> str:
    """生成会话 ID：时间戳 + 4 位随机十六进制后缀（防同秒撞车）。"""
    ts = (now or datetime.now()).strftime(constants.SESSION_ID_TIME_FMT)
    return f"{ts}-{secrets.token_hex(2)}"


def session_id_time(session_id: str) -> datetime | None:
    """从合法 session-id 解析其内嵌时间戳；格式非法返回 None。"""
    if not _SESSION_ID_RE.match(session_id):
        return None
    try:
        return datetime.strptime(session_id[:15], constants.SESSION_ID_TIME_FMT)
    except ValueError:
        return None


def _root(root: Path | str | None) -> Path:
    return Path(root) if root else constants.DEFAULT_ROOT


def project_dir_path(project_dir: Path | str, root: Path | str | None = None) -> Path:
    """项目级根目录：<root>/projects/<project-hash>。"""
    return _root(root) / constants.PROJECTS_DIR / project_hash(project_dir)


def session_memory_dir(
    project_dir: Path | str, session_id: str, root: Path | str | None = None
) -> Path:
    """单会话记忆目录：<root>/projects/<hash>/<session-id>/session-memory。"""
    return project_dir_path(project_dir, root) / session_id / constants.SESSION_MEMORY_DIR


def summary_path(
    project_dir: Path | str, session_id: str, root: Path | str | None = None
) -> Path:
    """单会话摘要文件路径。"""
    return session_memory_dir(project_dir, session_id, root) / constants.SUMMARY_FILENAME


def list_sessions(
    project_dir: Path | str, root: Path | str | None = None
) -> list[str]:
    """列出本项目下所有合法 session-id（按时间戳从新到旧排序）。"""
    base = project_dir_path(project_dir, root)
    if not base.exists():
        return []
    ids = [p.name for p in base.iterdir() if p.is_dir() and _SESSION_ID_RE.match(p.name)]
    # session-id 前缀零填充，字符串降序即时间从新到旧。
    return sorted(ids, reverse=True)


def latest_session(
    project_dir: Path | str,
    exclude: str | None = None,
    root: Path | str | None = None,
) -> str | None:
    """本项目最近一次会话 id（排除 exclude，通常为当前会话）；无则 None。"""
    for sid in list_sessions(project_dir, root):
        if sid != exclude:
            return sid
    return None


def cleanup_expired(
    project_dir: Path | str,
    retention_days: int,
    root: Path | str | None = None,
    now: datetime | None = None,
    exclude: str | None = None,
) -> list[str]:
    """删除超 retention_days 的会话目录（按 session-id 内嵌时间戳判龄）；返回被删 id 列表。

    时间戳无法解析的目录一律保留（不误删未知）；exclude（当前会话）始终保留。失败软化：
    单目录删除异常记告警、继续处理其余。
    """
    base = project_dir_path(project_dir, root)
    if not base.exists():
        return []
    cutoff = now or datetime.now()
    removed: list[str] = []
    for child in base.iterdir():
        if not child.is_dir() or child.name == exclude:
            continue
        ts = session_id_time(child.name)
        if ts is None:
            continue  # 不认识的目录不删
        if (cutoff - ts).total_seconds() > retention_days * 86400:
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as e:
                logger.warning("清理过期会话目录失败（保留）：%s：%s", child, e)
    return removed
