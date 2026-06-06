"""环境快照采集与格式化（见 #0006）。

会话启动时采集一次 cwd / OS / 日期 / git 分支 + 简短状态，缓存为只读快照（git 子进程**只调
一次**），格式化为带 `<env>` 标签的环境块。该块作为 `system` 尾部块放在稳定前缀断点**之后**
（不进可缓存前缀、不写入持久化历史）。git 不可用时降级标注，不抛错。
"""

import logging
import os
import platform
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)

# 环境块标签（见 checklist 固定值）。
ENV_TAG_OPEN = "<env>"
ENV_TAG_CLOSE = "</env>"

# git 不可用 / 非 git 仓库时的降级文案（见 checklist 固定值）。
GIT_UNAVAILABLE = "非 git 仓库 / 不可用"

# 单次 git 调用的超时（秒）：避免异常仓库挂死启动。
_GIT_TIMEOUT = 5


def _collect_git() -> str:
    """单次 git 子进程取分支 + 简短状态；不可用则降级，不抛错。

    用 `git status --short --branch` 一次拿全：首行 `## <branch>...` 给分支，其余行是简短改动。
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("git 不可用", exc_info=True)
        return GIT_UNAVAILABLE
    if proc.returncode != 0:
        return GIT_UNAVAILABLE

    lines = proc.stdout.splitlines()
    branch = "?"
    if lines and lines[0].startswith("##"):
        # `## main...origin/main [ahead 1]` → 取 `main`
        branch = lines[0][2:].strip().split("...")[0].split(" ")[0] or "?"
    changes = [ln for ln in lines[1:] if ln.strip()]
    if changes:
        return f"分支 {branch}，{len(changes)} 处未提交改动"
    return f"分支 {branch}，工作区干净"


def collect_env_snapshot() -> dict:
    """采集环境快照（git 子进程只调一次）；返回只读 dict。"""
    return {
        "cwd": os.getcwd(),
        "os": platform.platform(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "git": _collect_git(),
    }


def format_env_block(snapshot: dict) -> str:
    """把快照格式化为带 `<env>` 标签的环境块文本。"""
    return (
        f"{ENV_TAG_OPEN}\n"
        f"cwd: {snapshot.get('cwd', '?')}\n"
        f"os: {snapshot.get('os', '?')}\n"
        f"date: {snapshot.get('date', '?')}\n"
        f"git: {snapshot.get('git', GIT_UNAVAILABLE)}\n"
        f"{ENV_TAG_CLOSE}"
    )


def build_env_block() -> str:
    """便捷入口：采集 + 格式化（启动时调一次，整会话复用结果）。"""
    return format_env_block(collect_env_snapshot())
