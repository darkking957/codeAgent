"""`if` 条件编译与求值（#0013 T2）。

复用 #0007 的权限匹配器（``parse_rule`` / ``Rule.matches``）做条件判定，零新匹配模式：
  - 单条规则字符串：如 ``Bash(rm *)``、``Read(./.env)``；前缀 ``!`` 表示**反向取反**
    （命中条件 = 调用「不」匹配该规则）。
  - 多条组合：``{all: [...]}``（全部满足）或 ``{any: [...]}``（任一满足）二选一；同时含
    ``all`` 与 ``any`` 两键 → 配置错误（HookConfigError，由 loader fail-soft 跳过该规则）。

条件仅在携带工具信息（tool_name/tool_input）的事件上有意义；「非工具事件带 `if` → 静默跳过」
的语义在 HookManager 分发时落地（见 manager.py），本模块只管编译 + 对工具调用求值。
"""

from dataclasses import dataclass

from coreagent.errors import ConfigError
from coreagent.hooks.models import NEGATE_PREFIX, HookConfigError
from coreagent.permissions.rules import Rule, parse_rule


@dataclass
class Clause:
    """一条子条件：一条权限规则 + 是否反向取反。"""

    rule: Rule
    negate: bool = False

    def matches(self, tool_name: str, tool_input: dict) -> bool:
        m = self.rule.matches(tool_name, tool_input)
        return (not m) if self.negate else m


@dataclass
class Condition:
    """编译后的 `if` 条件：组合模式（single / all / any）+ 子条件列表。"""

    mode: str               # "single" | "all" | "any"
    clauses: list[Clause]

    def matches(self, tool_name: str, tool_input: dict) -> bool:
        """对一次工具调用求值；``any`` 任一命中即真，``single``/``all`` 须全部命中。"""
        results = (c.matches(tool_name, tool_input) for c in self.clauses)
        if self.mode == "any":
            return any(results)
        return all(results)


def _compile_clause(raw: str) -> Clause:
    """编译一条子条件字符串：剥离 `!` 反向前缀后复用 parse_rule（语法非法即抛）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise HookConfigError(f"条件项非法：{raw!r}（应为非空规则字符串，形如 Bash(rm *) 或 !Read(./public/*)）")
    s = raw.strip()
    negate = s.startswith(NEGATE_PREFIX)
    if negate:
        s = s[len(NEGATE_PREFIX):].strip()
        if not s:
            raise HookConfigError(f"反向条件缺少规则体：{raw!r}（`!` 之后须接一条规则，如 !Read(./public/*)）")
    try:
        rule = parse_rule(s)
    except ConfigError as e:
        # parse_rule 抛 PermissionConfigError（语法 / 未知别名）；包成 HookConfigError 让 loader 统一跳过。
        raise HookConfigError(f"条件规则非法：{raw!r}（{e}）") from e
    return Clause(rule=rule, negate=negate)


def compile_condition(raw) -> Condition | None:
    """把 `if` 原始值编译为 Condition；无 `if`（None）返回 None；结构非法抛 HookConfigError。

    接受：``None`` / 单条规则字符串 / 恰含一个 ``all`` 或 ``any`` 键的映射。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return Condition(mode="single", clauses=[_compile_clause(raw)])
    if isinstance(raw, dict):
        has_all = "all" in raw
        has_any = "any" in raw
        if has_all and has_any:
            raise HookConfigError("条件非法：if 不能同时含 all 与 any（二选一）")
        if not has_all and not has_any:
            raise HookConfigError(
                f"条件非法：if 映射须恰含一个 all 或 any 键，实为 {sorted(raw)!r}"
            )
        key = "all" if has_all else "any"
        items = raw[key]
        if not isinstance(items, list) or not items:
            raise HookConfigError(f"条件非法：if.{key} 须为非空列表，实为 {items!r}")
        return Condition(mode=key, clauses=[_compile_clause(s) for s in items])
    raise HookConfigError(
        f"条件非法：if 须为字符串或含 all/any 的映射，实为 {type(raw).__name__}"
    )
