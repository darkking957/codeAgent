"""T6｜后端检测 + 能力表（本期仅检测 + 失败语义，不落地真正窗格编排）。

后端取值：``auto`` / ``in-process`` / ``split-pane``。检测当前终端能力——能否分屏（tmux / iTerm2）、
是否处于不支持分屏的终端（VS Code / Windows Terminal / Ghostty）。

决策（``resolve_backend``）：
- 显式 ``split-pane`` + 缺 tmux/iTerm2（或不支持终端）→ 抛 ``BackendError``（硬失败、**不**降级）。
- 显式 ``split-pane`` + 环境支持但本期窗格编排未实现 → 抛 ``BackendError``（未实现，另开新号）。
- ``auto`` 命中分屏条件（在 tmux/iTerm2）→ 本期回落 ``in-process`` 并带提示。
- 其余 → ``in-process``。

本期**不**拉起真正的窗格 / 不按窗格 id 唤醒（见 spec Out of Scope）。
"""

import os
from dataclasses import dataclass

from coreagent.teams import constants


class BackendError(Exception):
    """后端决策硬失败（显式 split-pane 不满足条件 / 未实现）。绝不静默降级。"""


def _env(env: dict | None) -> dict:
    return env if env is not None else dict(os.environ)


def has_tmux(env: dict | None = None) -> bool:
    return bool(_env(env).get(constants.ENV_TMUX))


def has_iterm(env: dict | None = None) -> bool:
    return _env(env).get(constants.ENV_TERM_PROGRAM) == constants.TERM_ITERM


def is_split_capable(env: dict | None = None) -> bool:
    """当前终端是否具备分屏能力（tmux 或 iTerm2）。"""
    e = _env(env)
    return has_tmux(e) or has_iterm(e)


def is_unsupported_terminal(env: dict | None = None) -> bool:
    """是否处于明确不支持分屏的终端：VS Code / Windows Terminal / Ghostty。"""
    e = _env(env)
    term = e.get(constants.ENV_TERM_PROGRAM)
    if term == constants.TERM_VSCODE:
        return True
    if e.get(constants.ENV_WT_SESSION):
        return True
    if term == constants.TERM_GHOSTTY or e.get(constants.ENV_GHOSTTY_RESOURCES):
        return True
    return False


@dataclass
class BackendDecision:
    """后端决策结果：解析后的后端 + 可选提示（auto 回落时非空）。"""

    resolved: str
    notice: str = ""


def resolve_backend(requested: str = constants.BACKEND_AUTO, env: dict | None = None) -> BackendDecision:
    """据请求后端与终端能力产出决策；显式 split-pane 不满足/未实现 → 抛 BackendError。"""
    e = _env(env)
    if requested == constants.BACKEND_SPLIT_PANE:
        # 显式分屏：缺能力或处于不支持终端 → 硬失败（绝不降级）。
        if not is_split_capable(e) or is_unsupported_terminal(e):
            raise BackendError(constants.SPLITPANE_MISSING_ERROR)
        # 有能力但本期窗格编排未实现 → 明确「未实现，另开新号」错误。
        raise BackendError(constants.SPLITPANE_NOT_IMPLEMENTED_ERROR)

    if requested == constants.BACKEND_IN_PROCESS:
        return BackendDecision(resolved=constants.BACKEND_IN_PROCESS)

    # auto：命中分屏条件（在 tmux/iTerm2 且非不支持终端）→ 本期回落 in-process 并提示；否则 in-process。
    if is_split_capable(e) and not is_unsupported_terminal(e):
        return BackendDecision(
            resolved=constants.BACKEND_IN_PROCESS, notice=constants.AUTO_FALLBACK_NOTICE
        )
    return BackendDecision(resolved=constants.BACKEND_IN_PROCESS)
