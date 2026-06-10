"""MCP server 分层配置加载（#0008 T5；复用 #0007 作用域）。

server 列表声明在既有 `.coreagent/settings.json` 四作用域的 ``mcpServers`` 顶层键。复用
#0007 的 ``scope_paths()`` 取四作用域文件路径、复用其 fail-soft JSON 解析模式（坏文件跳过
该作用域 + 中文告警，不崩），并**记录每个 server 的来源作用域**（供 T8 安全门判定）。
**不动** #0007 既有的 permissions 解析——只多读一个顶层键。

合并优先级沿用 #0007：Managed > Project > User > Local；高优先作用域同名 server 覆盖低优先。

字段：``command`` 启动命令、``args`` 参数、``env`` 环境、``type``/``transport`` 传输类型、
``timeout`` 连接超时、``cwd`` 工作目录、``enabled`` 是否启用；值内 ``${ENV_VAR}`` 解析为环境变量。
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from coreagent.config import _resolve_env_vars
from coreagent.mcp import constants
from coreagent.permissions.scopes import SCOPE_ORDER, scope_paths

logger = logging.getLogger(__name__)

# 配置中读取 server 列表的顶层键。
MCP_SERVERS_KEY = "mcpServers"


@dataclass
class McpServerConfig:
    """单个 MCP server 的解析后配置。"""

    name: str
    command: str
    scope: str  # 来源作用域：managed / project / user / local（供安全门判定）
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    timeout: float = constants.CONNECT_TIMEOUT
    cwd: str | None = None
    enabled: bool = True


def _resolve(value) -> str:
    """把标量转字符串并解析其中的 ``${ENV_VAR}``（复用 #0006 / config.py 的解析）。"""
    return _resolve_env_vars(str(value))


def _parse_server(name: str, spec: dict, scope: str) -> McpServerConfig:
    """解析单个 server 声明；缺字段给默认，类型不符抛 ValueError（由上层 fail-soft 兜住整作用域）。"""
    if not isinstance(spec, dict):
        raise ValueError(f"server「{name}」声明须为对象，实为 {type(spec).__name__}")
    command = _resolve(spec.get("command", ""))
    raw_args = spec.get("args") or []
    if not isinstance(raw_args, list):
        raise ValueError(f"server「{name}」的 args 须为列表")
    args = [_resolve(a) for a in raw_args]
    raw_env = spec.get("env") or {}
    if not isinstance(raw_env, dict):
        raise ValueError(f"server「{name}」的 env 须为对象")
    env = {str(k): _resolve(v) for k, v in raw_env.items()}
    transport = str(spec.get("type") or spec.get("transport") or "stdio")
    try:
        timeout = float(spec.get("timeout", constants.CONNECT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = constants.CONNECT_TIMEOUT
    cwd = spec.get("cwd")
    cwd = _resolve(cwd) if cwd else None
    enabled = bool(spec.get("enabled", True))
    return McpServerConfig(
        name=name,
        command=command,
        scope=scope,
        args=args,
        env=env,
        transport=transport,
        timeout=timeout,
        cwd=cwd,
        enabled=enabled,
    )


def _load_scope_servers(path: Path, scope: str) -> dict[str, McpServerConfig]:
    """读单个作用域文件的 ``mcpServers``；缺文件 → 空；坏文件 → fail-soft（空 + 中文告警）。"""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层应为 JSON 对象（key: value）")
        raw = data.get(MCP_SERVERS_KEY) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{MCP_SERVERS_KEY} 字段须为对象")
        out: dict[str, McpServerConfig] = {}
        for name, spec in raw.items():
            out[str(name)] = _parse_server(str(name), spec, scope)
        return out
    except Exception as e:  # noqa: BLE001 —— fail-soft：坏文件跳过该作用域，不崩
        logger.warning(
            "MCP 配置文件解析失败（作用域 %s，路径 %s）：%s；已跳过该作用域，其余作用域仍生效",
            scope, path, e,
        )
        return {}


def load_mcp_servers(base_dir: Path | str) -> dict[str, McpServerConfig]:
    """加载四作用域的 ``mcpServers`` 并按优先级合并；返回 name → McpServerConfig。

    SCOPE_ORDER 高优先在前，先到先得（高优先同名覆盖低优先），与 #0007 合并语义一致。
    """
    paths = scope_paths(Path(base_dir))
    merged: dict[str, McpServerConfig] = {}
    for scope in SCOPE_ORDER:  # 高优先在前
        for name, cfg in _load_scope_servers(paths[scope], scope).items():
            if name not in merged:  # 高优先已占位 → 不被低优先覆盖
                merged[name] = cfg
    return merged
