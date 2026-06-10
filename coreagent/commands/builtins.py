"""T4｜内置命令的元数据与处理函数（#0011；#0012 调整：/review 移交技能、新增 /skill）。

按三类归位（见 spec 能力 9 / checklist 类型归属）：
  本地    ：/help /status /session /memory /permission /skill
  影响界面：/plan /do /mode /model /clear /compact
  提示词  ：（无内置；/review 由 #0012 review 样板技能接管，走技能次级来源分发）
另登记隐藏命令：/exit（别名 /quit）退出、/delegate 团队协调切换（#0017）——保留交互，但不进
补全 / 帮助、不计入可见命令。（/mode、/model 自 #0017 起改为可见，进补全 / 帮助。）

处理函数只经 UIControl 取能力 / 领域数据，不直接依赖任何终端渲染 / 输入补全框架。模式常量
取自 #0007（permissions.modes），属领域常量、非渲染框架，可直接引用。
"""

from coreagent.commands.registry import CommandRegistry
from coreagent.commands.types import CommandResult, CommandSpec, CommandType, UIControl
from coreagent.permissions import modes

# ── 固定文案（见 checklist 精确值；纯文本、不含富文本标记，便于精确断言与跨前端复用）──────
COMPACT_NO_CONTEXT = "未启用上下文管理，无法压缩"
MEMORY_NONE = "（暂无长期记忆）"
STATUS_NO_CONTEXT = "未启用上下文管理，无 token 统计"
CLEARED = "✓ 已清空会话历史"
PLAN_ENTERED = "已进入 plan 模式（只规划、不执行写操作）"
PLAN_EXITED = "已退出 plan 模式（切回 default）"
SKILL_NONE = "（暂无已发现技能）"
SKILL_NO_STORE = "未启用技能系统"
SKILL_UNKNOWN_SUB = "未知子命令：{sub}（可用：list / reload）"
# /skill 子命令（与 #0012 checklist 契约一致；命令层不反向依赖 skills 模块）。
SKILL_SUB_LIST = "list"
SKILL_SUB_RELOAD = "reload"
# /delegate 反馈文案（#0017 T6，见 checklist 精确值）。
DELEGATE_TEAMS_OFF = "团队功能未开启，无法切换 delegate"
DELEGATE_ON = "delegate 已开启"
DELEGATE_OFF = "delegate 已关闭"
# /model 文案。
MODEL_CURRENT = "当前模型：{model}（provider: {protocol}）"
MODEL_SWITCHED = "已切换模型 → {model}（本会话生效）"


def unknown_command_message(name: str) -> str:
    """未命中命令时的引导文案（含 /help 引导，见 checklist 精确值）。"""
    return f"未知命令：/{name}（输入 /help 查看可用命令）"


# ── 处理函数 ────────────────────────────────────────────────────────────────────


async def cmd_help(ui: UIControl, args: str) -> CommandResult:
    """由注册中心数据生成命令清单（非隐藏命令逐条；别名内联标注、不单列）。"""
    lines = ["可用命令："]
    for c in ui.list_commands():
        head = "/" + c.name + (f" {c.arg_hint}" if c.arg_hint else "")
        alias = f"（别名 {', '.join('/' + a for a in c.aliases)}）" if c.aliases else ""
        lines.append(f"  {head}{alias} — {c.summary}")
    ui.show("\n".join(lines))
    return CommandResult()


async def cmd_status(ui: UIControl, args: str) -> CommandResult:
    """token 用量统计（复用 #0009 format_stats，形如 x / y）。"""
    stats = ui.token_stats()
    ui.show(STATUS_NO_CONTEXT if stats is None else f"Token 用量：{stats}")
    return CommandResult()


async def cmd_session(ui: UIControl, args: str) -> CommandResult:
    """会话信息：消息条数 + session id（#0010）。"""
    info = ui.session_info()
    sid = info.get("session_id") or "(无)"
    ui.show(f"会话：消息 {info.get('count', 0)} 条 · session {sid}")
    return CommandResult()


async def cmd_memory(ui: UIControl, args: str) -> CommandResult:
    """长期记忆索引（复用 #0010 render_index）；无记忆 / 无 store 给提示。"""
    index = ui.memory_index()
    ui.show(index if index else MEMORY_NONE)
    return CommandResult()


async def cmd_permission(ui: UIControl, args: str) -> CommandResult:
    """当前模式 + 规则摘要（复用 #0007）。"""
    info = ui.permission_info()
    ui.show(
        f"当前模式：{info.get('mode')} · "
        f"规则 deny {info.get('deny', 0)} / ask {info.get('ask', 0)} / allow {info.get('allow', 0)}"
    )
    local = info.get("local_path")
    if local:
        ui.show(f"本地规则文件：{local}")
    return CommandResult()


async def cmd_plan(ui: UIControl, args: str) -> CommandResult:
    """进入计划模式（#0007 plan：写类拦截记为计划项）。"""
    ui.set_mode(modes.PLAN)
    ui.show(PLAN_ENTERED)
    return CommandResult()


async def cmd_do(ui: UIControl, args: str) -> CommandResult:
    """退回执行模式（#0007 default）。"""
    ui.set_mode(modes.DEFAULT)
    ui.show(PLAN_EXITED)
    return CommandResult()


async def cmd_clear(ui: UIControl, args: str) -> CommandResult:
    """清空会话历史并落盘。"""
    ui.clear_history()
    ui.show(CLEARED)
    return CommandResult()


async def cmd_compact(ui: UIControl, args: str) -> CommandResult:
    """手动压缩上下文（复用 #0009 before_request(manual=True)）。"""
    if not ui.has_context():
        ui.show(COMPACT_NO_CONTEXT)
        return CommandResult()
    summary = await ui.compact()
    ui.show(f"✓ 手动压缩完成：{summary}")
    return CommandResult()


async def cmd_skill(ui: UIControl, args: str) -> CommandResult:
    """技能管理（#0012）：`/skill list` 列出已发现技能、`/skill reload` 手动重扫（热更新）。"""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else SKILL_SUB_LIST
    if sub == SKILL_SUB_RELOAD:
        n = ui.reload_skills()
        ui.show(SKILL_NO_STORE if n is None else f"已重新扫描技能，共 {n} 个可用。")
        return CommandResult()
    if sub == SKILL_SUB_LIST:
        skills = ui.skills_overview()
        if skills is None:
            ui.show(SKILL_NO_STORE)
        elif not skills:
            ui.show(SKILL_NONE)
        else:
            lines = ["已发现技能（/技能名 触发激活并执行）："]
            for s in skills:
                mark = "●" if s.get("active") else "○"
                lines.append(f"  {mark} /{s['name']}（{s['mode']}）— {s['description']}")
            ui.show("\n".join(lines))
        return CommandResult()
    ui.show(SKILL_UNKNOWN_SUB.format(sub=sub))
    return CommandResult()


async def cmd_mode(ui: UIControl, args: str) -> CommandResult:
    """/mode 视图 / 切换（无参看当前模式 + 可选档集；带参切到指定权限模式）。"""
    ui.mode_command(args)
    return CommandResult()


async def cmd_model(ui: UIControl, args: str) -> CommandResult:
    """/model：无参查看当前模型；带参切换本会话使用的模型（运行时即时生效）。"""
    target = args.strip()
    if not target:
        info = ui.model_info()
        ui.show(MODEL_CURRENT.format(model=info.get("model"), protocol=info.get("protocol")))
        return CommandResult()
    ui.set_model(target)
    ui.show(MODEL_SWITCHED.format(model=target))
    return CommandResult()


async def cmd_delegate(ui: UIControl, args: str) -> CommandResult:
    """/delegate 切换团队协调模式（#0017 T6；取代 #0016 的 Shift+Tab 触发）。

    团队功能未开启 → 提示、不接管；开启时切换并按新状态反馈。
    """
    state = ui.toggle_delegate()
    if state is None:
        ui.show(DELEGATE_TEAMS_OFF)
    else:
        ui.show(DELEGATE_ON if state else DELEGATE_OFF)
    return CommandResult()


async def cmd_exit(ui: UIControl, args: str) -> CommandResult:
    """退出主循环（隐藏；别名 /quit）。"""
    return CommandResult(exit=True)


# ── 登记表（元数据单一来源）──────────────────────────────────────────────────────


def register_builtins(registry: CommandRegistry) -> None:
    """把全部内置命令登记进注册中心（顺序即 /help 展示顺序）。"""
    specs = [
        # ── 本地（只读 / 只打印，零 LLM）──────────────────────────────────────────
        CommandSpec("help", "查看可用命令", CommandType.LOCAL, cmd_help, aliases=("?",)),
        CommandSpec("status", "查看 token 用量统计", CommandType.LOCAL, cmd_status),
        CommandSpec("session", "查看当前会话信息", CommandType.LOCAL, cmd_session),
        CommandSpec("memory", "查看长期记忆索引", CommandType.LOCAL, cmd_memory),
        CommandSpec("permission", "查看当前权限模式与规则", CommandType.LOCAL, cmd_permission),
        CommandSpec("skill", "列出技能 / 重扫技能（list | reload）", CommandType.LOCAL,
                    cmd_skill, arg_hint="[list|reload]"),
        # ── 影响界面状态（改模式 / 切模型 / 清历史 / 压缩，零 LLM）────────────────────
        CommandSpec("plan", "进入计划模式（只规划、不执行写操作）", CommandType.AFFECTS_UI, cmd_plan),
        CommandSpec("do", "退回执行模式", CommandType.AFFECTS_UI, cmd_do),
        CommandSpec("mode", "查看 / 切换权限模式（acceptEdits / auto 等）", CommandType.AFFECTS_UI,
                    cmd_mode, arg_hint="[名]"),
        CommandSpec("model", "查看 / 切换模型", CommandType.AFFECTS_UI, cmd_model, arg_hint="[名]"),
        CommandSpec("clear", "清空会话历史", CommandType.AFFECTS_UI, cmd_clear),
        CommandSpec("compact", "手动压缩上下文（窄余量触发摘要）", CommandType.AFFECTS_UI, cmd_compact),
        # 注：/review 由 #0012 review 样板技能接管（走技能次级来源 /review），不再作为内置命令。
        # ── 隐藏（保留既有交互，不进补全 / 帮助、不计入可见命令）──────────────────────
        CommandSpec("exit", "退出 CoreAgent", CommandType.AFFECTS_UI, cmd_exit,
                    aliases=("quit",), hidden=True),
        # /delegate（#0017 T6）：团队协调模式切换（取代 #0016 的 Shift+Tab 触发）；隐藏，不计可见命令。
        CommandSpec("delegate", "切换团队协调（delegate）模式", CommandType.AFFECTS_UI,
                    cmd_delegate, hidden=True),
    ]
    for spec in specs:
        registry.register(spec)
