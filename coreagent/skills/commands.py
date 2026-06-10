"""T10｜技能作为命令的次级来源：补全候选 + 共享触发提示词 + 遮蔽检测（#0012）。

纯助手（不依赖任何渲染框架）：
- 技能名作为命令分发与补全的**次级来源**：内置命令名优先，冲突则技能被遮蔽（由 `shadowed_skills`
  报出，装配层据此 warning）。
- `/技能名 [参数]` 触发即激活并执行：共享模式回灌一段触发提示词（钉指令由激活块每轮承担），
  独立模式由 TUI 直接跑子对话（见 tui._dispatch_skill）。

实际的「激活 / 跑子对话」副作用留在 TUI（持 provider / registry / conversation）；本模块只产纯数据。
"""

from coreagent.skills.store import SkillStore


def skill_completions(
    store: SkillStore, prefix: str, taken: set[str] | None = None
) -> list[tuple[str, str]]:
    """技能名补全候选 ``[(/名称, 说明), ...]``：按前缀匹配；排除被内置命令遮蔽的名字。

    prefix 含前导 ``/``；taken 为已被内置命令占用的名字集合（命令名 + 别名），命中即跳过。
    """
    taken = taken or set()
    out: list[tuple[str, str]] = []
    for spec in store.list_skills():
        if spec.name in taken:
            continue
        full = "/" + spec.name
        if full.startswith(prefix):
            out.append((full, spec.description))
    return out


def shadowed_skills(store: SkillStore, taken: set[str]) -> list[str]:
    """返回名字与内置命令（名 / 别名）冲突、因而被遮蔽的技能名（装配层据此 warning）。"""
    return [spec.name for spec in store.list_skills() if spec.name in taken]


def shared_trigger_prompt(name: str, arguments: str = "") -> str:
    """共享模式 `/技能名` 触发时回灌的用户消息（完整 SOP 由每轮激活指令块承载，此处只下达触发）。"""
    base = f"请按已激活的技能「{name}」的指令执行当前任务。"
    arguments = (arguments or "").strip()
    return f"{base}（参数：{arguments}）" if arguments else base
