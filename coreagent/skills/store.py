"""T3/T5｜技能仓 SkillStore：发现映射 + 激活态 + 渲染块 + 白名单并集/校验 + 激活/注销/清空。

职责（仿 commands.CommandRegistry 的「单一来源仓」形态）：
- 持有「已发现技能映射」（reload 时三级发现 + 白名单校验得到）与「激活集（含激活时捕获的参数、
  按激活顺序）」。
- 渲染①启动目录块（名字 + 说明，走 system 尾部块）②每轮激活指令块（按激活顺序叠加、占位符
  已替换、可重复重渲染，走动态注入通道）。
- 计算激活白名单并集（各激活技能白名单 ∪ 各自专属工具名 ∪ 系统级 loader 恒在）。
- activate / deactivate / clear：激活时注册专属工具、注销时移除；clear 恢复全量工具。

白名单校验（T5）：reload 扫描期，每条白名单项对照「已知工具名（registry.names() ∪ loader）∪
该技能自带工具名」；非法 → 拒绝该技能并记含工具名的错误，**不退出进程、不阻断其他技能**。
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from coreagent.skills import constants
from coreagent.skills.discovery import discover_skills
from coreagent.skills.parser import render_body
from coreagent.skills.types import SkillSpec

logger = logging.getLogger(__name__)


@dataclass
class ActiveSkill:
    """一个激活中的技能：spec + 激活时捕获的参数 + 已注册的专属工具名（注销时移除）。"""

    spec: SkillSpec
    arguments: str = ""
    registered_tools: list[str] = field(default_factory=list)


class SkillStore:
    """技能仓：发现 + 校验 + 激活态管理 + 注入块渲染 + 白名单裁剪集计算。"""

    def __init__(self, registry, project_dir: Path | str | None = None) -> None:
        self.registry = registry
        self.project_dir = project_dir
        self._skills: dict[str, SkillSpec] = {}
        # 激活集：按激活顺序的列表（指令块叠加、白名单并集都依赖顺序/集合）。
        self._active: list[ActiveSkill] = []
        # reload 期被白名单校验拒绝的技能错误（含工具名），供 /skill 与单测观察。
        self.errors: list[str] = []
        self.reload()

    # ── 发现 + 白名单校验（含 T5）─────────────────────────────────────────────────

    def reload(self) -> int:
        """重新三级发现 + 白名单校验，刷新已发现映射；返回有效技能数。

        热更新入口（/skill reload）。已激活但发现后已消失的技能会被注销（恢复其工具可见性）；
        仍存在的保留激活态（沿用新 spec / 旧捕获参数）。
        """
        discovered = discover_skills(self.project_dir)
        known = set(self.registry.names()) | {constants.LOADER_TOOL_NAME}
        valid: dict[str, SkillSpec] = {}
        self.errors = []
        for name, spec in discovered.items():
            own = {t.name for t in spec.dedicated_tools}
            bad = [w for w in spec.allowed_tools if w not in known and w not in own]
            if bad:
                msg = f"技能「{name}」白名单引用不存在的工具：{', '.join(bad)}，已拒绝该技能"
                self.errors.append(msg)
                logger.warning(msg)
                continue
            valid[name] = spec
        self._skills = valid
        # 已激活但已消失的技能：注销（移除其专属工具、解除激活）。
        for active in list(self._active):
            if active.spec.name not in self._skills:
                self.deactivate(active.spec.name)
        return len(self._skills)

    # ── 只读访问 ─────────────────────────────────────────────────────────────────

    def get(self, name: str) -> SkillSpec | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills)

    def list_skills(self) -> list[SkillSpec]:
        """已发现技能（按名排序，稳定展示）。"""
        return [self._skills[n] for n in sorted(self._skills)]

    def is_active(self, name: str) -> bool:
        return any(a.spec.name == name for a in self._active)

    def active_names(self) -> list[str]:
        return [a.spec.name for a in self._active]

    # ── 激活 / 注销 / 清空 ────────────────────────────────────────────────────────

    def activate(self, name: str, arguments: str = "") -> SkillSpec:
        """激活指定技能（捕获参数、注册专属工具、加入激活集）；未知名抛 KeyError。

        幂等：已激活则只更新捕获参数（不重复注册）。多个技能可同时激活、按激活顺序叠加。
        """
        spec = self._skills.get(name)
        if spec is None:
            raise KeyError(f"未知技能：{name}")
        for active in self._active:
            if active.spec.name == name:
                active.arguments = arguments  # 重复激活：更新参数即可
                active.spec = spec
                return spec
        registered = self._register_dedicated(spec)
        self._active.append(ActiveSkill(spec=spec, arguments=arguments, registered_tools=registered))
        return spec

    def deactivate(self, name: str) -> None:
        """注销指定技能：移除其专属工具、从激活集删除（恢复其工具可见性）。"""
        for active in list(self._active):
            if active.spec.name == name:
                for tool_name in active.registered_tools:
                    self.registry.unregister(tool_name)
                self._active.remove(active)

    def clear(self) -> None:
        """清空全部激活技能：注销动态注册的专属工具、清空激活集（工具列表回全量）。"""
        for active in self._active:
            for tool_name in active.registered_tools:
                self.registry.unregister(tool_name)
        self._active.clear()

    def _register_dedicated(self, spec: SkillSpec) -> list[str]:
        """把目录型技能的专属工具注册进 registry；返回成功注册的工具名（供注销）。"""
        registered: list[str] = []
        for tool in spec.dedicated_tools:
            self.registry.unregister(tool.name)  # 防陈旧残留（热更新 / 重复激活）
            try:
                self.registry.register(tool)
                registered.append(tool.name)
            except ValueError as e:  # 与既有内置/MCP 工具或他技能工具重名 → 跳过该工具、不崩
                logger.warning("技能「%s」专属工具注册失败（跳过）：%s", spec.name, e)
        return registered

    # ── 白名单裁剪集 ─────────────────────────────────────────────────────────────

    def active_tool_filter(self) -> set[str] | None:
        """发给模型的工具名集合：无激活技能 → None（全量）；有 → 各白名单并集 ∪ 专属工具 ∪ loader。"""
        if not self._active:
            return None
        allow: set[str] = set()
        for active in self._active:
            allow.update(active.spec.allowed_tools)
            allow.update(t.name for t in active.spec.dedicated_tools)
        allow.add(constants.LOADER_TOOL_NAME)  # 系统级 loader 恒在、免白名单
        return allow

    # ── 注入块渲染 ───────────────────────────────────────────────────────────────

    def catalog_block(self) -> str | None:
        """启动目录块（名字 + 一句话说明，不含正文 SOP）；无技能 → None。

        作为 system 尾部块注入（不进可缓存前缀）；模型据此按需调 load_skill 激活。
        """
        if not self._skills:
            return None
        lines = [
            "以下技能可经 load_skill 工具按需激活（激活后其完整指令会注入上下文、工具列表收窄）："
        ]
        for spec in self.list_skills():
            lines.append(f"- {spec.name}：{spec.description}")
        body = "\n".join(lines)
        return f"{constants.CATALOG_TAG_OPEN}\n{body}\n{constants.CATALOG_TAG_CLOSE}"

    def render_instructions(self, name: str, arguments: str = "") -> str:
        """渲染单个技能的完整指令（正文 SOP，$ARGUMENTS 已替换）。"""
        spec = self._skills[name]
        return render_body(spec.body, arguments)

    def activation_block(self) -> str | None:
        """每轮激活指令块内容：按激活顺序叠加各技能渲染后 SOP；无激活技能 → None。

        返回带 <active-skill-instructions> 标签的文本；外层由注入层再裹 <system-reminder>
        （走动态通道、不打滚动缓存、每轮重建）。
        """
        if not self._active:
            return None
        sections: list[str] = ["你已激活以下技能，请严格按其指令执行当前任务："]
        for active in self._active:
            rendered = render_body(active.spec.body, active.arguments)
            sections.append(f"### 技能：{active.spec.name}\n{rendered}")
        body = "\n\n".join(sections)
        return f"{constants.ACTIVE_TAG_OPEN}\n{body}\n{constants.ACTIVE_TAG_CLOSE}"
