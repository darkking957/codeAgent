"""角色仓 AgentRoleStore：四级发现映射 + 只读查询 + 热更新（仿 #0012 SkillStore 的轻量版）。

角色无「激活态 / 专属工具 / 白名单注册校验」（那些是技能特有），故本仓只做：发现 → 持有映射 →
按名查询 / 列举 / reload。委派工具（#0014 T10）持本仓引用，按角色名取 AgentRole 起子 Agent。
"""

import logging
from pathlib import Path

from coreagent.agents.discovery import discover_roles
from coreagent.agents.types import AgentRole

logger = logging.getLogger(__name__)


class AgentRoleStore:
    """角色仓：四级发现 + 同名覆盖 + 只读查询 + reload。"""

    def __init__(self, project_dir: Path | str | None = None) -> None:
        self.project_dir = project_dir
        self._roles: dict[str, AgentRole] = {}
        self.reload()

    def reload(self) -> int:
        """重新四级发现，刷新已发现映射；返回有效角色数（热更新入口）。"""
        self._roles = discover_roles(self.project_dir)
        return len(self._roles)

    def get(self, name: str) -> AgentRole | None:
        return self._roles.get(name)

    def names(self) -> list[str]:
        return list(self._roles)

    def list_roles(self) -> list[AgentRole]:
        """已发现角色（按名排序，稳定展示）。"""
        return [self._roles[n] for n in sorted(self._roles)]
