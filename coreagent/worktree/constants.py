"""T1｜worktree 子系统固定值（非可调，改即改契约；spec 不写、checklist 即契约）。

集中落：worktrees 根相对位置 / 临时分支前缀 / 目录名字符集 / 长度上限 / 空闲清理阈值默认 /
git 子进程超时 / 自动收尾提交文案 / 周期清理节奏。改这些等于改契约，须同步 checklist。
"""

from coreagent.errors import CoreAgentError


class WorktreeError(CoreAgentError):
    """worktree 子系统领域异常：名校验失败 / 非 git 仓 / git 子进程失败 / 越界等。

    供上层（spawn_agent 委派）捕获后硬失败回灌——绝不静默回退到共享工作区。
    """


# ── 位置与命名（非可调）─────────────────────────────────────────────────────────
# worktrees 根：放仓库内**不被追踪**的位置（.git 下 git 从不追踪）。相对 repo_root。
WORKTREES_RELDIR = ".git/coreagent-worktrees"
# 临时分支前缀：每个隔离工作树各自一个临时分支，名 = 前缀 + worktree 名。
BRANCH_PREFIX = "coreagent/wt/"

# ── 目录名安全校验（非可调）─────────────────────────────────────────────────────
# 合法字符集（正则字符类内容）：字母数字 + 点 / 下划线 / 连字符 / 斜杠（斜杠做嵌套）。
NAME_CHARSET = "A-Za-z0-9._/-"
# 目录名长度上限。
NAME_MAX_LEN = 128

# ── 阈值 / 超时（默认值；cleanup_idle_hours 可经 WorktreeConfig 覆盖）──────────────
# 空闲清理阈值默认值（小时）：临时工作树空闲超此值且无变更保护触发即可被周期清理回收。
DEFAULT_CLEANUP_IDLE_HOURS = 24
# 周期清理任务的检查节奏（秒）：主循环启动后每隔该秒数扫一次（非清理阈值本身）。
CLEANUP_INTERVAL_SECONDS = 3600
# 单次 git 子进程超时（秒）：避免异常仓库挂死。
GIT_TIMEOUT = 30

# ── 收尾自动提交文案（非可调）───────────────────────────────────────────────────
AUTO_COMMIT_MESSAGE = "coreagent: 子 Agent 隔离工作树自动收尾提交"
