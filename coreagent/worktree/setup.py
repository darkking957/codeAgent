"""T6｜worktree 创建后环境初始化（由配置驱动，缺省走内置默认）。

补上「被 gitignore 忽略但运行时需要」的东西，让隔离工作树能直接跑起来：
- 软链 `symlink_dirs`（默认 node_modules / .venv）：源目录存在才链，省去重装大依赖。
- 复制 `copy_globs`（默认 .env / .env.*）：本地配置文件按 glob 复制进工作树。
- `link_git_hooks` 为真：把工作树的 hooksPath 指向主仓 hooks，使其提交跑同一套钩子。

纪律：源缺失 / 单条失败一律软化跳过、不抛错、不阻断创建（隔离工作区本身已可用）。
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from coreagent.worktree.constants import GIT_TIMEOUT

logger = logging.getLogger(__name__)


def setup_environment(worktree_path: Path | str, repo_root: Path | str, config) -> None:
    """按配置初始化工作树环境（软链 / 复制 / 接 hooks）；整体软化、不阻断创建。

    ``config`` 鸭子类型：读 ``symlink_dirs`` / ``copy_globs`` / ``link_git_hooks``（缺省安全）。
    """
    wt = Path(worktree_path)
    repo = Path(repo_root)
    _symlink_dirs(wt, repo, list(getattr(config, "symlink_dirs", []) or []))
    _copy_globs(wt, repo, list(getattr(config, "copy_globs", []) or []))
    if getattr(config, "link_git_hooks", False):
        _link_git_hooks(wt, repo)


def _symlink_dirs(wt: Path, repo: Path, dirs: list[str]) -> None:
    """逐个软链大型依赖目录；源不存在 / 目标已存在 / 单条失败 → 跳过。"""
    for d in dirs:
        try:
            src = repo / d
            if not src.is_dir():
                continue
            dst = wt / d
            if dst.exists() or dst.is_symlink():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(src.resolve(), dst)
        except OSError as e:
            logger.warning("worktree 软链 %s 失败（跳过）：%r", d, e)


def _copy_globs(wt: Path, repo: Path, globs: list[str]) -> None:
    """按 glob 把本地配置文件复制进工作树（保持相对路径）；单条失败 → 跳过。"""
    for pattern in globs:
        try:
            for src in repo.glob(pattern):
                if not src.is_file():
                    continue
                dst = wt / src.relative_to(repo)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except OSError as e:
            logger.warning("worktree 复制 %s 失败（跳过）：%r", pattern, e)


def _link_git_hooks(wt: Path, repo: Path) -> None:
    """把工作树 core.hooksPath 指向主仓 hooks 目录，使其提交跑同一套钩子；失败软化。"""
    hooks_dir = repo / ".git" / "hooks"
    if not hooks_dir.is_dir():
        return
    try:
        subprocess.run(
            ["git", "-C", str(wt), "config", "core.hooksPath", str(hooks_dir)],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("worktree 接入 git hooks 失败（跳过）：%r", e)
