"""四类动作执行 + 退出码语义 + 失败隔离（#0013 T3）。

动作类型：
  - command：事件载荷以 JSON 经 stdin 传入；以退出码裁决、stdout/stderr 回话。
  - prompt ：产出注入正文（直接走旁路注入）。
  - http   ：发请求；响应不参与控制（非 2xx / 连接失败只记日志）。
  - subagent：占位——仅记日志，不真实启动子 Agent（见 spec Out of Scope）。

退出码语义（command）：
  - ``0``（继续码）→ 放行 / 继续；stdout（若有）作注入正文。
  - ``2``（拦截码）→ **仅拦截类事件（is_blocking）生效**：硬拦，stdout+stderr 作拒绝理由回灌；
    非拦截类事件上的 ``2`` 按非阻塞错误处理（记日志、不拦、不注入）。
  - 其余（``1`` / 崩溃 / 超时）→ 非阻塞错误：记日志后继续，目标工具照跑 / 事件继续。

**失败隔离（核心安全属性）**：``execute_action`` 绝不上抛——任何崩溃 / 超时 / HTTP 失败一律
吞成一条 ``ActionResult``（必要时记日志），保证 Hook 失败永不中断主流程。
"""

import json
import logging
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from coreagent.hooks.models import (
    ACTION_COMMAND,
    ACTION_HTTP,
    ACTION_PROMPT,
    ACTION_SUBAGENT,
    EXIT_BLOCK,
    EXIT_CONTINUE,
    Action,
)

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """一条动作的执行结果。``blocked`` 仅对拦截类事件有意义。"""

    blocked: bool = False    # 是否硬拦（仅命令动作 exit 2 + 拦截类事件）
    reason: str = ""         # 拦截理由（回灌模型）
    inject: str = ""         # 注入正文（prompt 正文 / 命令 stdout）


def _command(action: Action, payload: dict, *, is_blocking: bool, timeout: int) -> ActionResult:
    """命令动作：stdin 收 JSON 载荷，按退出码语义裁决；任何失败 → 非阻塞 ActionResult。"""
    try:
        stdin_data = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        stdin_data = json.dumps({"event": payload.get("event", "")})
    try:
        proc = subprocess.run(
            action.command,
            shell=True,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Hook 命令动作超时（%ss），按非阻塞错误处理：%s", timeout, action.command)
        return ActionResult()
    except Exception as e:  # noqa: BLE001 —— 启动失败 / 崩溃：非阻塞错误，绝不上抛
        logger.warning("Hook 命令动作执行失败，按非阻塞错误处理：%r（命令=%s）", e, action.command)
        return ActionResult()

    code = proc.returncode
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if code == EXIT_CONTINUE:
        return ActionResult(inject=out)  # 放行；stdout 作注入正文
    if code == EXIT_BLOCK and is_blocking:
        reason = out or err or "Hook 命令以拦截码（2）硬拦该操作"
        return ActionResult(blocked=True, reason=reason)
    # 其余退出码（含非拦截事件上的 2）→ 非阻塞错误。
    logger.warning(
        "Hook 命令动作非零退出（code=%s，非阻塞处理）：%s；stderr=%s",
        code, action.command, err[:200],
    )
    return ActionResult()


def _prompt(action: Action) -> ActionResult:
    """提示词动作：正文直接作注入文本。"""
    return ActionResult(inject=(action.prompt or "").strip())


def _http(action: Action, payload: dict, timeout: int) -> ActionResult:
    """HTTP 动作：发请求；响应不参与控制（非 2xx / 连接失败只记日志、不拦、不注入）。"""
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        body = b"{}"
    method = (action.method or "POST").upper()
    try:
        req = urllib.request.Request(
            action.url,
            data=body if method != "GET" else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if not (200 <= int(status) < 300):
                logger.warning("Hook HTTP 动作非 2xx（status=%s）：%s", status, action.url)
    except urllib.error.HTTPError as e:
        logger.warning("Hook HTTP 动作返回错误状态（%s）：%s", e.code, action.url)
    except Exception as e:  # noqa: BLE001 —— 连接失败 / 超时等：只记日志，主流程不受影响
        logger.warning("Hook HTTP 动作请求失败：%r（url=%s）", e, action.url)
    return ActionResult()


def _subagent(action: Action, payload: dict) -> ActionResult:
    """子 Agent 动作（占位）：仅记日志，不真实启动（见 spec Out of Scope）。"""
    logger.info(
        "Hook subagent 动作（占位，本期不真实启动）：event=%s subagent=%s",
        payload.get("event"), action.subagent,
    )
    return ActionResult()


def execute_action(
    action: Action,
    payload: dict,
    *,
    is_blocking: bool = False,
    timeout: int = 60,
) -> ActionResult:
    """执行一条动作，返回 ActionResult。**绝不上抛**：任何异常吞成空 ActionResult 并记日志。"""
    try:
        if action.type == ACTION_COMMAND:
            return _command(action, payload, is_blocking=is_blocking, timeout=timeout)
        if action.type == ACTION_PROMPT:
            return _prompt(action)
        if action.type == ACTION_HTTP:
            return _http(action, payload, timeout=timeout)
        if action.type == ACTION_SUBAGENT:
            return _subagent(action, payload)
    except Exception as e:  # noqa: BLE001 —— 失败隔离：动作任何异常都不许中断主流程
        logger.warning("Hook 动作执行异常（type=%s），已隔离：%r", action.type, e)
    return ActionResult()
