"""T5｜worktree 生命周期（创建 / 快速恢复 / 删除 / 收尾）+ 变更保护。

git 子进程统一在此封装（仿 `environment.py:_collect_git` 的调用范式：固定 argv、超时、异常软化为
领域错误）。所有路径显式传 cwd，不 chdir。

- 创建：校验名 → 目录已存在则**快速恢复**（只 stat、不调 git）→ 否则确认在 git 仓后
  `git worktree add -b <临时分支> <路径>`，再跑环境初始化。非 git 仓 / 创建失败 → 抛
  WorktreeError（供上层硬失败，绝不静默回退共享工作区）。
- 变更保护：删除前若有未提交改动、或临时分支有「未被基线（HEAD）合并」的 commit → 默认拒删
  （`force=True` 覆盖）。不按 upstream 判（临时分支通常无 upstream），用 `merge-base --is-ancestor`
  判 tip 是否已被 HEAD 收割。
- 收尾 finalize：子 Agent 结束时调——有未提交改动则 `git add -A` + commit 到临时分支并保留；
  干净但有未收割 commit 则保留（待上层 git merge）；无任何改动则删除工作树。
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from coreagent.worktree.constants import (
    AUTO_COMMIT_MESSAGE,
    BRANCH_PREFIX,
    GIT_TIMEOUT,
    WORKTREES_RELDIR,
    WorktreeError,
)
from coreagent.worktree.names import resolve_worktree_path
from coreagent.worktree.setup import setup_environment

logger = logging.getLogger(__name__)


@dataclass
class Worktree:
    """一个隔离工作树的句柄（manager 产出、runner 收尾消费）。"""

    name: str
    path: Path        # 工作树绝对路径（= 子 Agent 的 cwd）
    branch: str       # 临时分支名（前缀 + name）
    recovered: bool = False   # True = 快速恢复（复用已存在目录、未调 git 新建）


@dataclass
class RemovalResult:
    """一次删除的结果（被变更保护拦下时 removed=False、reason 说明原因）。"""

    removed: bool
    reason: str = ""


@dataclass
class FinalizeResult:
    """一次收尾的结果：action ∈ committed（提交并保留）/ kept（有未收割 commit 保留）/ removed（无改动删除）。"""

    action: str
    worktree: Worktree


class WorktreeManager:
    """同一仓库内的多工作树能力封装：建 / 恢复 / 删 / 收尾 + 变更保护 + 清理支撑。"""

    def __init__(self, repo_root: Path | str, config=None) -> None:
        self.repo_root = Path(repo_root).resolve()
        if config is None:
            # 延迟导入避免 config → worktree.constants → worktree 包的潜在环依赖。
            from coreagent.config import WorktreeConfig

            config = WorktreeConfig()
        self.config = config
        # worktrees 根 = repo_root/.git/coreagent-worktrees（git 从不追踪 .git 下内容）。
        self.root = self.repo_root / WORKTREES_RELDIR

    # ── git 子进程封装（固定 argv、shell=False、超时、异常软化为领域错误）─────────────
    def _git(self, *args: str, cwd: Path | str | None = None, check: bool = False):
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd else str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise WorktreeError(f"git 子进程调用失败：{e}") from e
        if check and proc.returncode != 0:
            raise WorktreeError(
                f"git {' '.join(args[:2])} 失败（exit {proc.returncode}）：{proc.stderr.strip()}"
            )
        return proc

    def _ensure_git_repo(self) -> None:
        """确认 repo_root 是 git 工作区；否则硬失败（不静默建目录）。"""
        proc = self._git("rev-parse", "--is-inside-work-tree")
        if proc.returncode != 0 or proc.stdout.strip() != "true":
            raise WorktreeError(
                f"当前目录不是 git 工作区（{self.repo_root}），"
                f"无法创建 worktree 隔离工作区（声明 isolation=worktree 的角色委派将硬失败）"
            )

    # ── 创建 / 快速恢复 ────────────────────────────────────────────────────────────
    def create(self, name: str) -> Worktree:
        """建一个隔离工作树（含快速恢复）；非 git 仓 / 创建失败抛 WorktreeError。"""
        path = resolve_worktree_path(self.root, name)
        branch = BRANCH_PREFIX + name
        # 快速恢复：目标目录已存在 → 只 stat、不调 git、直接复用（同名再进入幂等、省 git 开销）。
        if path.exists():
            return Worktree(name=name, path=path, branch=branch, recovered=True)
        # 硬失败前置：非 git 仓在 mkdir 之前就报错，避免静默留下空目录。
        self._ensure_git_repo()
        self.root.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-b", branch, str(path), check=True)
        try:
            setup_environment(path, self.repo_root, self.config)
        except Exception as e:  # noqa: BLE001 —— 环境初始化整体兜底，不阻断创建
            logger.warning("worktree 环境初始化失败（已忽略，工作树仍可用）：%r", e)
        return Worktree(name=name, path=path, branch=branch, recovered=False)

    # ── 变更保护 ──────────────────────────────────────────────────────────────────
    def _has_uncommitted(self, wt: Worktree) -> bool:
        """工作树是否有未提交改动（porcelain 非空；尊重 .gitignore，软链 / .env 等忽略项不计）。"""
        return bool(self._git("status", "--porcelain", cwd=wt.path).stdout.strip())

    def _has_unmerged_commits(self, wt: Worktree) -> bool:
        """临时分支 tip 是否**未被** HEAD 收割：is-ancestor rc=0 已合并、rc=1 未合并、其它（分支缺失等）当未合并的反面处理。"""
        proc = self._git("merge-base", "--is-ancestor", wt.branch, "HEAD")
        return proc.returncode == 1

    def change_guard(self, wt: Worktree) -> str | None:
        """变更保护判定：被拦返回原因文案，放行返回 None。"""
        if self._has_uncommitted(wt):
            return "工作树有未提交改动"
        if self._has_unmerged_commits(wt):
            return f"临时分支「{wt.branch}」有未被基线（HEAD）合并的 commit"
        return None

    # ── 删除 ──────────────────────────────────────────────────────────────────────
    def remove(self, wt: Worktree, *, force: bool = False) -> RemovalResult:
        """删除工作树 + 临时分支；默认先过变更保护（被拦返回 removed=False），force 覆盖。"""
        if not force:
            reason = self.change_guard(wt)
            if reason:
                return RemovalResult(removed=False, reason=reason)
        if not wt.path.exists():
            # 目录已不在：清登记 + 删分支即可。
            self._git("worktree", "prune")
            self._git("branch", "-D", wt.branch)
            return RemovalResult(removed=True)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(wt.path))
        self._git(*args, check=True)
        # 删临时分支（-D 强删：未合并也删，删的是隔离分支不影响主线）。
        self._git("branch", "-D", wt.branch)
        return RemovalResult(removed=True)

    # ── 收尾（子 Agent 结束时调，正常 / 取消 / 报错一视同仁）───────────────────────────
    def finalize(self, wt: Worktree) -> FinalizeResult:
        """按变更情况决定去留：有改动→自动提交并保留；干净但有未收割 commit→保留；无改动→删除。"""
        try:
            if self._has_uncommitted(wt):
                self._git("add", "-A", cwd=wt.path)
                self._git("commit", "-m", AUTO_COMMIT_MESSAGE, cwd=wt.path)
                return FinalizeResult(action="committed", worktree=wt)
            if self._has_unmerged_commits(wt):
                return FinalizeResult(action="kept", worktree=wt)
            self.remove(wt, force=True)
            return FinalizeResult(action="removed", worktree=wt)
        except WorktreeError as e:  # 收尾失败保守保留（不丢工作）
            logger.warning("worktree 收尾失败（保留工作树）：%r", e)
            return FinalizeResult(action="kept", worktree=wt)

    # ── 清理支撑（供后台周期清理用）────────────────────────────────────────────────
    def registered_worktree_paths(self) -> list[Path]:
        """`git worktree list --porcelain` 中登记的全部工作树路径（含主仓）；非 git 仓返回空。"""
        proc = self._git("worktree", "list", "--porcelain")
        if proc.returncode != 0:
            return []
        return [
            Path(line[len("worktree "):].strip())
            for line in proc.stdout.splitlines()
            if line.startswith("worktree ")
        ]

    def worktree_from_path(self, path: Path | str) -> Worktree:
        """由 worktrees 根内的路径还原句柄（name = 相对根、branch = 前缀 + name）。"""
        p = Path(path).resolve()
        name = p.relative_to(self.root.resolve()).as_posix()
        return Worktree(name=name, path=p, branch=BRANCH_PREFIX + name)
