"""T7｜系统级 loader 工具 load_skill：模型按需调用以激活指定技能。

契约要点：
- 工具名精确为 load_skill；入参 = 技能名 + 可选参数（捕获为 $ARGUMENTS）。
- 调用即激活该技能（共享语义：钉指令 + 收窄工具，激活态持续到清空对话）。
- 该工具**恒在**裁剪后的工具列表中、**不受任何白名单约束**（由 SkillStore.active_tool_filter
  把 LOADER_TOOL_NAME 始终并入实现；本工具自身不计入白名单校验）。
- 免确认（激活无破坏性副作用，仅改可见工具 + 注入指令）：归读类、可并发、不弹确认。

激活后的完整指令经「每轮激活指令块」注入上下文，故本工具结果只回确认 + 收窄后的可用工具，
不重复回灌整段 SOP（避免与激活块重复）。
"""

from coreagent.skills import constants
from coreagent.skills.store import SkillStore
from coreagent.skills.types import SkillMode
from coreagent.tools.base import Tool, ToolResult


class LoadSkillTool(Tool):
    """激活技能的系统级工具（持 SkillStore 引用）。"""

    name = constants.LOADER_TOOL_NAME
    description = (
        "激活一个已登记的技能：加载其完整 SOP 指令到上下文、把可用工具收窄到该技能白名单。"
        "需要执行某项已沉淀的流程（如提交、评审、跑测试）时调用；技能名取自上下文中的技能目录。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要激活的技能名（须在技能目录中）"},
            "arguments": {
                "type": "string",
                "description": "可选参数，整体替换技能正文里的 $ARGUMENTS 占位符",
            },
        },
        "required": ["name"],
    }
    requires_confirmation = False

    def __init__(self, store: SkillStore) -> None:
        self._store = store

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        # cwd（#0015）：本工具不涉路径，忽略即可（签名兼容 registry 的 kwarg 透传）。
        name = str(arguments.get("name", "")).strip()
        if not name:
            return ToolResult.fail("load_skill 失败：缺少技能名 name")
        spec = self._store.get(name)
        if spec is None:
            known = "、".join(self._store.names()) or "（无）"
            return ToolResult.fail(f"load_skill 失败：未知技能「{name}」。可用技能：{known}")
        args = str(arguments.get("arguments", "") or "")
        try:
            self._store.activate(name, args)
        except KeyError:
            return ToolResult.fail(f"load_skill 失败：未知技能「{name}」")
        allowed = self._store.active_tool_filter() or set()
        tools_hint = "、".join(sorted(allowed)) if allowed else "（全量）"
        mode_hint = "独立" if spec.mode is SkillMode.INDEPENDENT else "共享"
        return ToolResult.ok(
            f"已激活技能「{name}」（{mode_hint}模式）。其完整指令已注入上下文，请按其执行。"
            f"当前可用工具：{tools_hint}。"
        )
