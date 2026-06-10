"""技能系统（#0012）固定值集中落此（spec 不写、checklist 即契约）：目录约定 / frontmatter
字段名 / 占位符 / 默认值 / loader 工具名 / 注入标签。改这些等于改契约，须同步 checklist。
"""

from pathlib import Path

# ── 三级目录约定（项目 > 用户 > 内置）────────────────────────────────────────────────
# 项目级：相对项目根（字面串便于 grep 校验：.coreagent/skills）。
PROJECT_SKILLS_RELDIR = ".coreagent/skills"
# 用户级：~/.coreagent/skills。
USER_SKILLS_DIR = Path("~/.coreagent/skills").expanduser()
# 内置级：包内 coreagent/skills/builtins/。
BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtins"

# ── 单文件 / 目录型识别 ───────────────────────────────────────────────────────────
# 单文件技能后缀。
SKILL_FILE_SUFFIX = ".md"
# 目录型技能入口文件名（目录内含此文件即认定为目录型技能）。
SKILL_ENTRY_FILENAME = "SKILL.md"
# 目录型技能的专属工具实现脚本名（importlib 进程内加载）。
SKILL_IMPL_FILENAME = "impl.py"

# ── frontmatter 字段名（精确，见 checklist 契约）──────────────────────────────────
FIELD_NAME = "name"
FIELD_DESCRIPTION = "description"
FIELD_ALLOWED_TOOLS = "allowed-tools"
FIELD_MODE = "mode"
FIELD_MAX_HISTORY_TOKENS = "max-history-tokens"
FIELD_MODEL = "model"

# ── 执行模式取值（精确）──────────────────────────────────────────────────────────
MODE_SHARED = "shared"
MODE_INDEPENDENT = "independent"

# ── 参数占位符（激活时整体替换）──────────────────────────────────────────────────
ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"

# ── 独立模式带入历史默认预算（未声明 max-history-tokens 时）───────────────────────
DEFAULT_MAX_HISTORY_TOKENS = 4000

# ── 系统级 loader 工具名（恒在、免白名单）─────────────────────────────────────────
LOADER_TOOL_NAME = "load_skill"

# ── 管理命令 ─────────────────────────────────────────────────────────────────────
SKILL_COMMAND = "skill"
SKILL_SUBCOMMAND_LIST = "list"
SKILL_SUBCOMMAND_RELOAD = "reload"

# ── 注入标签 ─────────────────────────────────────────────────────────────────────
# 启动目录块（system 尾部块、不进可缓存前缀；同 <env> / <project-instructions>）。
CATALOG_TAG_OPEN = "<available-skills>"
CATALOG_TAG_CLOSE = "</available-skills>"
# 每轮激活指令块（走动态注入通道、外层再裹 <system-reminder>，故不打滚动缓存）。
ACTIVE_TAG_OPEN = "<active-skill-instructions>"
ACTIVE_TAG_CLOSE = "</active-skill-instructions>"

# ── 独立模式回流摘要抬头（主历史中的那条摘要消息前缀）────────────────────────────
INDEPENDENT_SUMMARY_HEADER = "【独立技能「{name}」执行摘要】"
# 独立模式子对话带入的近段主历史抬头。
INDEPENDENT_CONTEXT_HEADER = "【主对话近段历史（供参考）】"
