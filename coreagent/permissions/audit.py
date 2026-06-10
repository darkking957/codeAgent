"""结构化决策日志（#0007 T8）。

每次工具调用经流水线后落一条结构化记录：工具名、参数快照（超长截断）、命中阶段、命中规则、
最终决策、作用域、模式。记录以 JSON 形式经项目 logging（#0002 引入）落 stderr，便于后续
规则优化与回溯消费；同时存内存列表供测试断言。
"""

import json
import logging

from coreagent.permissions.rules import Decision

# 参数快照单值最大字符数：超长截断加 `…`，避免日志吐巨量内容（如写文件全文）。
MAX_SNAPSHOT_CHARS = 200


class AuditLog:
    """决策审计 sink：record 一次写一条结构化记录。"""

    def __init__(self, logger: logging.Logger | None = None,
                 max_chars: int = MAX_SNAPSHOT_CHARS) -> None:
        self._logger = logger or logging.getLogger("coreagent.permissions.audit")
        self._max_chars = max_chars
        self.entries: list[dict] = []

    def _snapshot(self, tool_input: dict) -> dict:
        """参数快照：逐值字符串化并截断（超长 → 前 N 字符 + `…(+M)`）。"""
        snap: dict = {}
        if not isinstance(tool_input, dict):
            return {"_raw": self._clip(str(tool_input))}
        for k, v in tool_input.items():
            snap[k] = self._clip(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))
        return snap

    def _clip(self, s: str) -> str:
        if len(s) > self._max_chars:
            return s[: self._max_chars] + f"…(+{len(s) - self._max_chars})"
        return s

    def record(self, tool_name: str, tool_input: dict, decision: Decision,
               *, mode: str | None = None) -> dict:
        """记录一条决策；返回该结构化记录（便于调用方 / 测试使用）。"""
        entry = {
            "tool": tool_name,
            "input": self._snapshot(tool_input),
            "stage": decision.stage,
            "rule": decision.matched_rule,
            "outcome": decision.outcome,
            "scope": decision.scope,
            "mode": mode,
        }
        self.entries.append(entry)
        self._logger.info("permission_decision %s",
                          json.dumps(entry, ensure_ascii=False, default=str))
        return entry
