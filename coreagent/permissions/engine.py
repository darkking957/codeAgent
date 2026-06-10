"""规则引擎求值（#0007 T3）。

``evaluate(tool_name, tool_input, mode) -> Decision``：

  按类型优先 deny → ask → allow，各类型内首匹配即止；
  未命中任何规则 → 默认 **ask**（fail-closed），绝不静默 allow。

「Managed 不可被低级覆盖」由 deny 类型优先求值实现（任一 deny 命中即拒，不再看 allow）。
「升档丢弃宽泛规则」在 allow 阶段实现：当 mode 处于升档（≥ acceptEdits）时，跳过非预置
（scope != builtin）的宽泛 allow 规则（如 `Bash(*)`），精确规则保留。该过滤是即时计算、
不改基线，故降档后宽泛规则自动恢复。
"""

from coreagent.permissions import modes
from coreagent.permissions.rules import (
    ALLOW,
    ASK,
    DENY,
    SCOPE_BUILTIN,
    Decision,
    Rule,
    split_command,
)
from coreagent.permissions.scopes import MergedRules


class PermissionEngine:
    """持有合并后的规则集，按 deny→ask→allow 首匹配求值。"""

    def __init__(self, merged: MergedRules) -> None:
        self._merged = merged

    @property
    def default_mode(self) -> str:
        return modes.normalize_mode(self._merged.default_mode)

    @property
    def merged(self) -> MergedRules:
        """合并后的规则集（只读，供 /permission 等展示规则摘要）。"""
        return self._merged

    def evaluate(self, tool_name: str, tool_input: dict, mode: str) -> Decision:
        escalated = modes.is_escalated(mode)
        # 命令类（run_command）按子命令求值：链式命令不能靠单条前缀 allow 整体放行。
        if tool_name == "run_command":
            command = ""
            if isinstance(tool_input, dict):
                command = str(tool_input.get("command") or "")
            return self._evaluate_command(command, escalated)
        return self._evaluate_simple(tool_name, tool_input, escalated)

    def _allow_rules(self, escalated: bool) -> list[Rule]:
        """生效的 allow 规则；升档时丢弃非预置的宽泛规则（精确 + builtin 预置保留）。"""
        if not escalated:
            return self._merged.allow
        return [r for r in self._merged.allow
                if not (r.is_broad and r.scope != SCOPE_BUILTIN)]

    def _evaluate_simple(self, tool_name: str, tool_input: dict, escalated: bool) -> Decision:
        for rule in self._merged.deny:
            if rule.matches(tool_name, tool_input):
                return Decision(DENY, stage="rules", matched_rule=rule.raw,
                                scope=rule.scope, reason=f"命中 deny 规则 {rule.raw}")
        for rule in self._merged.ask:
            if rule.matches(tool_name, tool_input):
                return Decision(ASK, stage="rules", matched_rule=rule.raw,
                                scope=rule.scope, reason=f"命中 ask 规则 {rule.raw}")
        for rule in self._allow_rules(escalated):
            if rule.matches(tool_name, tool_input):
                return Decision(ALLOW, stage="rules", matched_rule=rule.raw,
                                scope=rule.scope, reason=f"命中 allow 规则 {rule.raw}")
        return Decision(ASK, stage="rules", reason="未命中任何规则，默认询问（fail-closed）")

    def _evaluate_command(self, command: str, escalated: bool) -> Decision:
        """命令裁决：deny / ask 命中整条或任一子命令即生效；allow 需**每个子命令**都被覆盖。"""
        subs = split_command(command)
        # deny / ask 检查整条命令 + 各子命令（任一命中即生效，倾向更严）。
        targets = [command, *subs]

        def hits(rule: Rule) -> bool:
            return any(rule.matches("run_command", {"command": t}) for t in targets)

        for rule in self._merged.deny:
            if hits(rule):
                return Decision(DENY, stage="rules", matched_rule=rule.raw,
                                scope=rule.scope, reason=f"命中 deny 规则 {rule.raw}")
        for rule in self._merged.ask:
            if hits(rule):
                return Decision(ASK, stage="rules", matched_rule=rule.raw,
                                scope=rule.scope, reason=f"命中 ask 规则 {rule.raw}")

        allow_rules = self._allow_rules(escalated)
        first_match: Rule | None = None
        for sub in subs:
            covering = next(
                (r for r in allow_rules if r.matches("run_command", {"command": sub})), None
            )
            if covering is None:
                return Decision(ASK, stage="rules",
                                reason="命令含未被 allow 覆盖的子命令，默认询问（fail-closed）")
            first_match = first_match or covering
        # subs 恒非空（split_command 至少返回一段），故走到此处 first_match 必已赋值。
        assert first_match is not None
        return Decision(ALLOW, stage="rules", matched_rule=first_match.raw,
                        scope=first_match.scope,
                        reason=f"命中 allow 规则 {first_match.raw}（全部子命令已覆盖）")
