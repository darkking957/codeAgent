"""T2｜worktree 目录名安全校验（纯函数）。

防 LLM / 角色输入触发路径遍历逃逸：限字符集与长度上限、拒绝任何 `.`/`..`/空 段、允许 `/`
做嵌套；再把名挂到 worktrees 根下解析，断言结果严格落在根内（双保险，防字符集漏网）。
"""

import re
from pathlib import Path

from coreagent.worktree.constants import NAME_CHARSET, NAME_MAX_LEN, WorktreeError

# 整名匹配：仅允许字符集内字符（一个或多个）。空格 / ; / | / $ 等均不在集内 → 拒绝。
_NAME_RE = re.compile(rf"^[{NAME_CHARSET}]+$")


def validate_name(name: str) -> str:
    """校验 worktree 目录名；合法则原样返回，任何不合法处抛 WorktreeError。

    规则：非空、不超长、仅含字符集内字符、任一以 `/` 切分的段不得为空 / `.` / `..`。
    """
    if not isinstance(name, str) or not name.strip():
        raise WorktreeError("worktree 名不能为空")
    if len(name) > NAME_MAX_LEN:
        raise WorktreeError(f"worktree 名超长（上限 {NAME_MAX_LEN}）：{len(name)} 字符")
    if not _NAME_RE.match(name):
        raise WorktreeError(f"worktree 名含非法字符（仅允许 {NAME_CHARSET}）：{name!r}")
    for seg in name.split("/"):
        if seg in ("", ".", ".."):
            raise WorktreeError(f"worktree 名含非法路径段（空 / . / ..）：{name!r}")
    return name


def resolve_worktree_path(root: Path | str, name: str) -> Path:
    """先校验名，再把名挂到 worktrees 根下解析；断言解析结果严格位于根内（双保险）。

    返回该 worktree 的绝对路径；任何越界（解析后落到根外或正好等于根）抛 WorktreeError。
    """
    validate_name(name)
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / name).resolve()
    if candidate == root_resolved or not candidate.is_relative_to(root_resolved):
        raise WorktreeError(
            f"worktree 名解析后越界（须严格位于 {root_resolved} 内）：{name!r}"
        )
    return candidate
