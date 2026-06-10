"""T7｜后台周期清理：回收过期临时工作树，删前过**三层过滤**保证安全。

三层（任一不满足则跳过保留）：
  ① 路径：解析为绝对路径且**严格位于 worktrees 根内**（根外的主仓 / 别处工作树跳过）。
  ② 登记：出现在 `git worktree list` 结果中（根内的「野目录」不经 git remove，不误删）。
  ③ 状态：空闲超阈值（按目录 mtime 判龄）且**不触发变更保护**（无未提交改动 / 无未收割 commit）。

只迭代 git 登记的工作树（层②内建），逐个过层① / 层③ 后才 remove。仿 memory 的启动期过期清理范式。
"""

import logging
import time
from pathlib import Path

from coreagent.worktree.constants import DEFAULT_CLEANUP_IDLE_HOURS, WorktreeError

logger = logging.getLogger(__name__)


def _within_root(path: Path | str, root_resolved: Path) -> bool:
    """层①：path 解析后严格位于 root 内（且不等于 root 本身）。"""
    try:
        rp = Path(path).resolve()
    except OSError:
        return False
    return rp != root_resolved and rp.is_relative_to(root_resolved)


def _is_idle(path: Path | str, idle_hours: float, now: float) -> bool:
    """层③（龄）：目录 mtime 距 now 超过空闲阈值（小时）。"""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return False
    return (now - mtime) >= idle_hours * 3600


def cleanup_idle_worktrees(
    manager,
    *,
    now: float | None = None,
    idle_hours: float | None = None,
) -> list[str]:
    """扫 git 登记的工作树，对 worktrees 根内、空闲超阈值、无变更保护触发者回收；返回被删路径列表。"""
    root = manager.root
    try:
        if not root.exists():
            return []
    except OSError:
        return []
    if idle_hours is None:
        idle_hours = getattr(manager.config, "cleanup_idle_hours", DEFAULT_CLEANUP_IDLE_HOURS)
    if now is None:
        now = time.time()

    root_resolved = root.resolve()
    removed: list[str] = []
    # 层②：候选仅取 git 登记的工作树（野目录天然不在其中、不会被 git remove）。
    for path in manager.registered_worktree_paths():
        if not _within_root(path, root_resolved):     # 层①
            continue
        if not _is_idle(path, idle_hours, now):        # 层③（龄）
            continue
        wt = manager.worktree_from_path(path)
        if manager.change_guard(wt) is not None:       # 层③（变更保护）
            continue
        try:
            if manager.remove(wt, force=False).removed:
                removed.append(str(path))
        except WorktreeError as e:
            logger.warning("worktree 周期清理删除失败（跳过）：%s：%r", path, e)
    return removed
