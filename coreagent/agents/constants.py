"""子 Agent 委派系统（#0014）固定值集中落此（spec 不写、checklist 即契约）：目录约定 /
frontmatter 字段名 / 委派工具名 / 类型取值 / 后台后缀标签 / 阈值。改这些等于改契约，须同步
checklist。

沿用 #0012 技能系统的三级目录范式，并新增「插件级」槽位（本仓暂无插件子系统，预留不扫描）。
"""

from pathlib import Path

# ── 四级目录约定（项目 > 用户 > 内置 > 插件）────────────────────────────────────────
# 项目级：相对项目根（字面串便于 grep 校验：.coreagent/agents）。
PROJECT_AGENTS_RELDIR = ".coreagent/agents"
# 用户级：~/.coreagent/agents。
USER_AGENTS_DIR = Path("~/.coreagent/agents").expanduser()
# 内置级：包内 coreagent/agents/builtins/。
BUILTIN_AGENTS_DIR = Path(__file__).parent / "builtins"
# 插件级：预留槽位。本仓无插件子系统（已 grep 确认），当前**不扫描任何插件目录**；
# 待插件系统落地后在此接入其角色来源目录（TODO）。
PLUGIN_AGENTS_DIRS: list[Path] = []  # TODO(#0014 插件级预留)：插件系统落地后填充其角色目录

# ── 角色文件识别 ─────────────────────────────────────────────────────────────────
# 角色定义文件后缀（Markdown + YAML frontmatter）。
AGENT_FILE_SUFFIX = ".md"

# ── frontmatter 字段名（精确，见 checklist 契约；kebab-case 对齐 #0012 风格）─────────
FIELD_NAME = "name"
FIELD_DESCRIPTION = "description"
FIELD_ALLOWED_TOOLS = "allowed-tools"
FIELD_DENIED_TOOLS = "denied-tools"
FIELD_MODEL = "model"
FIELD_MAX_TURNS = "max-turns"
FIELD_PERMISSION_MODE = "permission-mode"
# 隔离声明（#0015）：角色 frontmatter 声明隔离需求；仅认 worktree 取值，其余 / 缺省 → 无隔离。
FIELD_ISOLATION = "isolation"
ISOLATION_WORKTREE = "worktree"

# ── 委派工具（统一工具，对主 Agent 始终可见、schema 稳定）────────────────────────────
DELEGATION_TOOL_NAME = "spawn_agent"

# ── 委派类型取值（type 入参，精确）─────────────────────────────────────────────────
TYPE_DEFINITIONAL = "definitional"   # 定义式：空白对话 + 指定角色
TYPE_FORK = "fork"                   # Fork 式：继承父历史与工具集、强制后台、不套角色

# ── 后台结果系统提示后缀块标签（不进可缓存前缀；同 <env> / <available-skills>）─────────
BACKGROUND_RESULTS_TAG_OPEN = "<background-agent-results>"
BACKGROUND_RESULTS_TAG_CLOSE = "</background-agent-results>"

# ── 前台超时自动转后台阈值（秒）：前台子 Agent 跑超此阈值则 detach 转后台 ───────────────
FOREGROUND_TIMEOUT_SECONDS = 120.0

# ── 后台子 Agent 转后台后的工具白名单（第三层过滤；只读类，避免后台无人值守做破坏性写）──
BACKGROUND_TOOL_ALLOWLIST = frozenset({"read_file", "glob", "grep", "run_command"})

# ── 子 Agent 未产出文本摘要时的占位（仿 #0012 独立模式）─────────────────────────────
EMPTY_SUMMARY_PLACEHOLDER = "（子 Agent 未产出文本摘要）"
