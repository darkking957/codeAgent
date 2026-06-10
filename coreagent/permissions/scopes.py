"""四级作用域加载与合并（#0007 T2）。

四个独立规则文件（JSON），优先级 Managed > Project > User > Local：
  Managed = /etc/coreagent/managed-settings.json（可被 $COREAGENT_MANAGED_SETTINGS 覆盖路径）
  User    = ~/.config/coreagent/settings.json
  Project = <repo>/.coreagent/settings.json
  Local   = <repo>/.coreagent/settings.local.json   （本地临时覆盖，不进 Git）

文件格式：``{"permissions": {"deny": [...], "ask": [...], "allow": [...]},
"defaultMode": "...", "allowManagedPermissionRulesOnly": false}``，各字段缺省有容错默认。

容错与 fail-closed：
  - 缺文件 → 该作用域为空（不报错）。
  - 解析失败（坏 JSON / 非法规则）→ 该作用域**按「无 allow」处理**（整段丢弃，不放行），
    记一条可定位的中文告警，整体不崩。
  - allowManagedPermissionRulesOnly=true（Managed 侧）→ 仅 Managed 规则生效，忽略
    Project/User/Local 及 builtin 预置（组织锁定）。
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from coreagent.permissions.modes import MODES
from coreagent.permissions.rules import PermissionConfigError, Rule, parse_rule

logger = logging.getLogger(__name__)

# 作用域名（也是 Rule.scope 取值）。
SCOPE_MANAGED = "managed"
SCOPE_PROJECT = "project"
SCOPE_USER = "user"
SCOPE_LOCAL = "local"
# 合并顺序（优先级高在前；同类型规则首匹配即止，故高作用域先被检查）。
SCOPE_ORDER = [SCOPE_MANAGED, SCOPE_PROJECT, SCOPE_USER, SCOPE_LOCAL]

MANAGED_ENV = "COREAGENT_MANAGED_SETTINGS"
DEFAULT_MANAGED_PATH = "/etc/coreagent/managed-settings.json"


def scope_paths(base_dir: Path) -> dict[str, Path]:
    """四作用域文件路径（base_dir = 项目 / repo 根）。"""
    base_dir = Path(base_dir)
    managed = os.environ.get(MANAGED_ENV) or DEFAULT_MANAGED_PATH
    return {
        SCOPE_MANAGED: Path(managed),
        SCOPE_USER: Path("~/.config/coreagent/settings.json").expanduser(),
        SCOPE_PROJECT: base_dir / ".coreagent" / "settings.json",
        SCOPE_LOCAL: base_dir / ".coreagent" / "settings.local.json",
    }


def local_settings_path(base_dir: Path) -> Path:
    """Local 作用域文件路径（persist 落盘目标）。"""
    return scope_paths(base_dir)[SCOPE_LOCAL]


@dataclass
class ScopeConfig:
    """单个作用域解析后的规则与设置。"""

    deny: list[Rule] = field(default_factory=list)
    ask: list[Rule] = field(default_factory=list)
    allow: list[Rule] = field(default_factory=list)
    default_mode: str | None = None
    managed_only: bool = False


@dataclass
class MergedRules:
    """合并后的三类规则集（含 builtin）与全局设置。"""

    deny: list[Rule] = field(default_factory=list)
    ask: list[Rule] = field(default_factory=list)
    allow: list[Rule] = field(default_factory=list)
    default_mode: str | None = None
    managed_only: bool = False


def _parse_rule_list(items, scope: str) -> list[Rule]:
    """解析一类规则列表；任一条非法即抛（由 load_scope fail-closed 兜住整段）。"""
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError(f"permissions 字段须为列表，实为 {type(items).__name__}")
    return [parse_rule(s, scope=scope) for s in items]


def load_scope(path: Path, scope: str) -> ScopeConfig:
    """加载单个作用域文件；缺文件 → 空；解析失败 → 空（无 allow，fail-closed）+ 中文告警。"""
    path = Path(path)
    if not path.exists():
        return ScopeConfig()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层应为 JSON 对象（key: value）")
        perms = data.get("permissions") or {}
        if not isinstance(perms, dict):
            raise ValueError("permissions 字段须为对象")
        default_mode = data.get("defaultMode")
        if default_mode is not None and (not isinstance(default_mode, str)
                                         or default_mode not in MODES):
            # 非法 defaultMode 不致命：忽略并回落（仍给出可定位中文告警，不静默吞）。
            logger.warning(
                "权限配置 defaultMode 非法（作用域 %s，路径 %s）：%r；已忽略，回落默认模式。"
                "合法值：%s", scope, path, default_mode, "/".join(MODES),
            )
            default_mode = None
        return ScopeConfig(
            deny=_parse_rule_list(perms.get("deny"), scope),
            ask=_parse_rule_list(perms.get("ask"), scope),
            allow=_parse_rule_list(perms.get("allow"), scope),
            default_mode=default_mode,
            managed_only=bool(data.get("allowManagedPermissionRulesOnly", False)),
        )
    except Exception as e:  # noqa: BLE001 —— 解析失败 fail-closed：该作用域不贡献任何 allow
        logger.warning(
            "权限规则文件解析失败（作用域 %s，路径 %s）：%s；"
            "该作用域按「无可用 allow」处理（不放行），请修复后重试",
            scope, path, e,
        )
        return ScopeConfig()


def _pick_default_mode(scopes: dict[str, ScopeConfig]) -> str | None:
    """defaultMode 优先级：Managed > Project > User > Local；都未设则 None。"""
    for name in SCOPE_ORDER:
        mode = scopes[name].default_mode
        if mode:
            return mode
    return None


def load_merged(base_dir: Path, *, defaults: list[Rule] | None = None) -> MergedRules:
    """加载四作用域并合并；managed_only 时仅 Managed 生效（忽略其余 + builtin 预置）。"""
    paths = scope_paths(base_dir)
    scopes = {name: load_scope(paths[name], name) for name in SCOPE_ORDER}

    managed_only = scopes[SCOPE_MANAGED].managed_only
    active = [SCOPE_MANAGED] if managed_only else SCOPE_ORDER

    deny: list[Rule] = []
    ask: list[Rule] = []
    allow: list[Rule] = []
    for name in active:  # 按 SCOPE_ORDER 高优先在前
        sc = scopes[name]
        deny.extend(sc.deny)
        ask.extend(sc.ask)
        allow.extend(sc.allow)

    # builtin 预置接在 allow 末尾（最低优先）；锁定模式下连预置也不生效。
    if not managed_only and defaults:
        allow.extend(defaults)

    return MergedRules(
        deny=deny,
        ask=ask,
        allow=allow,
        default_mode=_pick_default_mode(scopes),
        managed_only=managed_only,
    )


def _atomic_write_json(path: Path, data: dict) -> None:
    """同目录临时文件 → fsync → 原子替换（仿 conversation.save，写中断不损坏既有文件）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def persist_allow_rule(rule_str: str, path: Path) -> None:
    """把一条 allow 规则原文写入指定作用域文件（默认 Local）：读旧 JSON → 追加去重 → 原子写。"""
    path = Path(path)
    data: dict = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("顶层应为 JSON 对象")
            data = loaded
        except (json.JSONDecodeError, OSError, ValueError) as e:
            # 现有文件损坏：**不覆盖**（否则会抹掉用户手写的其它规则）；报错保留原文件，待修复。
            raise PermissionConfigError(
                f"无法写入规则：现有文件 {path} 解析失败（{e}）；已保留原文件，请先修复后重试"
            ) from e
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        perms = data["permissions"] = {}
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        allow = perms["allow"] = []
    if rule_str not in allow:
        allow.append(rule_str)
    _atomic_write_json(path, data)
