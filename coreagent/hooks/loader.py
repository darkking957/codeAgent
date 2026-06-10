"""hooks.yaml 加载 + 集中校验（#0013 T1）。

两档配置（仿 #0007 作用域多档合并）：
  Project = <base_dir>/.coreagent/hooks.yaml
  User    = ~/.config/coreagent/hooks.yaml
两档规则合并为单一列表（Project 在前、User 在后），统一交 HookManager 分发。

文件格式::

    hooks:
      - event: PreToolUse
        if: "Bash(rm *)"
        action: {type: command, command: "./guard.sh"}
        timeout: 30
      - event: SessionStart
        action: {type: prompt, prompt: "生产环境，谨慎操作"}

加载期集中校验（任一不合法 → 记日志含该条原文 + 原因，**跳过该条**，其余照常加载，绝不崩溃）：
  - 缺 ``event`` 或缺 ``action`` → 非法。
  - 未知事件名（不在 10 个白名单内）→ 非法。
  - 未知动作类型（不在 command/prompt/http/subagent 内）→ 非法。
  - ``async: true`` 用于拦截类事件（PreToolUse / PreCompact）→ 非法（拦截类禁止异步）。
  - ``if`` 结构非法（双键 all/any、规则语法错误、未知别名）→ 非法。
  - 缺文件 / 空文件 → 该档零规则（不报错）。
"""

import logging
from pathlib import Path

import yaml

from coreagent.hooks.conditions import compile_condition
from coreagent.hooks.models import (
    ACTION_COMMAND,
    ACTION_HTTP,
    ACTION_PROMPT,
    ACTION_TYPES,
    BLOCKING_EVENTS,
    DEFAULT_TIMEOUT,
    EVENTS,
    Action,
    Hook,
    HookConfigError,
)

logger = logging.getLogger(__name__)

USER_HOOKS_PATH = Path("~/.config/coreagent/hooks.yaml").expanduser()


def hooks_paths(base_dir: Path) -> list[tuple[str, Path]]:
    """两档 hooks.yaml 路径（Project 在前、User 在后）。"""
    base_dir = Path(base_dir)
    return [
        ("project", base_dir / ".coreagent" / "hooks.yaml"),
        ("user", USER_HOOKS_PATH),
    ]


def _build_action(spec: dict) -> Action:
    """从 action 片段构建 Action；类型缺失 / 非法 / 必填字段缺失 → HookConfigError。"""
    if not isinstance(spec, dict):
        raise HookConfigError(f"action 须为映射，实为 {type(spec).__name__}")
    atype = spec.get("type")
    if atype not in ACTION_TYPES:
        raise HookConfigError(
            f"未知动作类型：{atype!r}；合法类型：{'/'.join(sorted(ACTION_TYPES))}"
        )
    if atype == ACTION_COMMAND:
        cmd = spec.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise HookConfigError("command 动作缺少非空 command 字段")
        return Action(type=atype, command=cmd)
    if atype == ACTION_PROMPT:
        prompt = spec.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HookConfigError("prompt 动作缺少非空 prompt 字段")
        return Action(type=atype, prompt=prompt)
    if atype == ACTION_HTTP:
        url = spec.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HookConfigError("http 动作缺少非空 url 字段")
        method = spec.get("method", "POST")
        if not isinstance(method, str) or not method.strip():
            raise HookConfigError(f"http 动作 method 非法：{method!r}")
        return Action(type=atype, url=url, method=method.strip().upper())
    # subagent（占位）：不强制字段，留描述。
    sub = spec.get("subagent") or spec.get("name")
    return Action(type=atype, subagent=str(sub) if sub is not None else None)


def _build_hook(spec: dict, source: str) -> Hook:
    """从单条规则片段构建 Hook；任一项不合法 → HookConfigError（loader 据此跳过该条）。"""
    if not isinstance(spec, dict):
        raise HookConfigError(f"规则须为映射，实为 {type(spec).__name__}")

    event = spec.get("event")
    if not event:
        raise HookConfigError("规则缺少 event 字段")
    if event not in EVENTS:
        raise HookConfigError(
            f"未知事件名：{event!r}；合法事件：{', '.join(sorted(EVENTS))}"
        )

    if "action" not in spec or spec.get("action") is None:
        raise HookConfigError("规则缺少 action 字段")
    action = _build_action(spec["action"])

    # 执行控制：only-once / async / timeout。
    only_once = bool(spec.get("only_once", False))
    async_ = bool(spec.get("async", False))
    if async_ and event in BLOCKING_EVENTS:
        raise HookConfigError(
            f"拦截类事件禁止异步：event={event} 不可设 async: true"
            f"（拦截裁决必须同步等待）"
        )
    timeout = spec.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise HookConfigError(f"timeout 须为正数，实为 {timeout!r}")

    # `if` 条件（可省）：编译期校验结构（双键 / 语法 / 未知别名都在此抛）。
    condition = compile_condition(spec.get("if"))

    return Hook(
        event=event,
        action=action,
        condition=condition,
        only_once=only_once,
        async_=async_,
        timeout=int(timeout),
        source=source,
        raw=spec,
    )


def _load_one(path: Path, source: str) -> list[Hook]:
    """加载单档 hooks.yaml；缺文件 → 空；逐条校验，非法条记日志后跳过、合法条照常返回。"""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("hooks 配置读取失败（档 %s，路径 %s）：%s；该档按零规则处理", source, path, e)
        return []
    if not isinstance(data, dict):
        logger.warning("hooks 配置顶层须为映射（档 %s，路径 %s），按零规则处理", source, path)
        return []
    raw_hooks = data.get("hooks")
    if raw_hooks is None:
        return []
    if not isinstance(raw_hooks, list):
        logger.warning("hooks 字段须为列表（档 %s，路径 %s），按零规则处理", source, path)
        return []

    hooks: list[Hook] = []
    for spec in raw_hooks:
        try:
            hooks.append(_build_hook(spec, source))
        except HookConfigError as e:
            # 非法规则记日志（含该条原文 + 原因）后跳过；其余规则照常加载。
            logger.warning("跳过非法 Hook 规则（档 %s）：%s；原文=%r", source, e, spec)
    return hooks


def load_hooks(base_dir: Path) -> list[Hook]:
    """加载两档 hooks.yaml 并合并为单一规则列表（Project 在前、User 在后）。

    缺文件 / 空文件 → 该档零规则；任一非法规则记日志后跳过、合法规则照常生效；绝不因配置崩溃。
    """
    hooks: list[Hook] = []
    for source, path in hooks_paths(base_dir):
        hooks.extend(_load_one(path, source))
    return hooks
