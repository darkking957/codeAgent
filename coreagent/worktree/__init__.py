"""Git Worktree 隔离子系统（#0015）。

给每个声明了隔离的子 Agent 在**同一仓库**里开一个独立工作目录（Git Worktree：共享版本库、各自
一个临时分支、各自一棵工作树），让并行子 Agent 能同时改文件互不干扰。纯 Git 工作树能力，独立于
agents 子系统。

模块：
  constants  固定值（根位置 / 分支前缀 / 字符集 / 长度上限 / 阈值 / 超时）+ WorktreeError
  names      目录名安全校验（防路径遍历逃逸）
  manager    生命周期（建 / 快速恢复 / 删 / 收尾）+ 变更保护 + 清理支撑
  setup      创建后环境初始化（软链 / 复制 / 接 git hooks，由配置驱动）
  cleanup    后台周期清理 + 三层过滤
"""

from coreagent.worktree.cleanup import cleanup_idle_worktrees
from coreagent.worktree.constants import (
    BRANCH_PREFIX,
    CLEANUP_INTERVAL_SECONDS,
    DEFAULT_CLEANUP_IDLE_HOURS,
    NAME_MAX_LEN,
    WORKTREES_RELDIR,
    WorktreeError,
)
from coreagent.worktree.manager import (
    FinalizeResult,
    RemovalResult,
    Worktree,
    WorktreeManager,
)
from coreagent.worktree.names import resolve_worktree_path, validate_name
from coreagent.worktree.setup import setup_environment

__all__ = [
    "WorktreeError",
    "WORKTREES_RELDIR",
    "BRANCH_PREFIX",
    "NAME_MAX_LEN",
    "DEFAULT_CLEANUP_IDLE_HOURS",
    "CLEANUP_INTERVAL_SECONDS",
    "validate_name",
    "resolve_worktree_path",
    "WorktreeManager",
    "Worktree",
    "RemovalResult",
    "FinalizeResult",
    "setup_environment",
    "cleanup_idle_worktrees",
]
