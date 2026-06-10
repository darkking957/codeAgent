"""记忆系统（#0010）固定文案 / 阈值 / 枚举（spec 砍掉的具体值集中落此，便于 checklist 对齐）。"""

# ── 记忆根目录 ─────────────────────────────────────────────────────────────────────
# 默认根（可被 config.memory.root 覆盖）。会话摘要、项目级笔记按项目哈希分目录落此之下。
from pathlib import Path

DEFAULT_ROOT = Path("~/.coreagent").expanduser()

# 项目哈希算法：项目绝对路径的 sha256 十六进制前 16 位（同项目稳定、不同项目互异）。
PROJECT_HASH_LEN = 16

# 会话存储相对布局：<root>/projects/<hash>/<session-id>/session-memory/summary.md
PROJECTS_DIR = "projects"
SESSION_MEMORY_DIR = "session-memory"
SUMMARY_FILENAME = "summary.md"

# 会话 ID 格式：YYYYMMDD-HHMMSS-xxxx（4 位随机十六进制后缀防同秒撞车）。
SESSION_ID_TIME_FMT = "%Y%m%d-%H%M%S"
SESSION_ID_REGEX = r"^\d{8}-\d{6}-[a-z0-9]{4}$"

# ── 项目指令文件（四层）────────────────────────────────────────────────────────────
# 指令文件名（项目级 / 各层统一用此名）。
INSTRUCTION_FILENAME = "COREAGENT.md"
# 本地个人级指令文件名（不入库，进 .gitignore）。
LOCAL_INSTRUCTION_FILENAME = "COREAGENT.local.md"
# 组织级 / 用户级固定路径。
ORG_INSTRUCTION_PATH = Path("/etc/coreagent") / INSTRUCTION_FILENAME
USER_INSTRUCTION_PATH = (Path("~/.coreagent") / INSTRUCTION_FILENAME).expanduser()
# 项目级目录内备选路径（相对项目根）：优先 ./COREAGENT.md，否则 ./.coreagent/COREAGENT.md。
PROJECT_INSTRUCTION_RELS = (INSTRUCTION_FILENAME, str(Path(".coreagent") / INSTRUCTION_FILENAME))

# 指令块标签：作为 system 上下文块注入；含 cwd 相关层，故装配时**不打**跨会话稳定缓存（同 <env>）。
INSTRUCTIONS_TAG_OPEN = "<project-instructions>"
INSTRUCTIONS_TAG_CLOSE = "</project-instructions>"

# ── @include 递归展开 ──────────────────────────────────────────────────────────────
INCLUDE_DIRECTIVE = "@include"
# 深度超限 / 越界错误文案前缀（checklist 断言用；完整文案后接具体路径 / 深度）。
INCLUDE_DEPTH_ERROR = "@include 嵌套深度超过上限（{max_depth} 层），已停止展开：{path}"
INCLUDE_ESCAPE_ERROR = "@include 路径越界（超出项目目录），已拒绝：{path}"

# ── 自动笔记（四类 / 两级）─────────────────────────────────────────────────────────
# 四类笔记的 frontmatter type 取值（顺序固定）。
NOTE_TYPES = ("user", "feedback", "project", "reference")
# type → 中文类别名（用户偏好 / 纠正反馈 / 项目知识 / 参考资料）。
NOTE_TYPE_LABELS = {
    "user": "用户偏好",
    "feedback": "纠正反馈",
    "project": "项目知识",
    "reference": "参考资料",
}
# type → 存储级别：用户偏好/纠正反馈 = 用户级（全局）；项目知识/参考资料 = 项目级（按哈希）。
NOTE_LEVEL_USER = "user"
NOTE_LEVEL_PROJECT = "project"
NOTE_TYPE_LEVEL = {
    "user": NOTE_LEVEL_USER,
    "feedback": NOTE_LEVEL_USER,
    "project": NOTE_LEVEL_PROJECT,
    "reference": NOTE_LEVEL_PROJECT,
}
# 笔记目录布局：用户级 = <root>/memory/notes/；项目级 = <root>/projects/<hash>/memory/notes/。
NOTES_RELDIR = str(Path("memory") / "notes")
NOTES_INDEX_FILENAME = "INDEX.md"

# ── 会话恢复 ───────────────────────────────────────────────────────────────────────
# 恢复摘要注入的 token 预算：超此即先压缩（复用 #0009），仍超则截断兜底，保证注入不超预算。
RESUME_TOKEN_BUDGET = 4000
# 时间跨度提醒文案（含固定前缀，checklist 断言用）。
RESUME_GAP_REMINDER = "距上次会话已超过 {hours} 小时，其间项目可能已变化，请按需重新读取文件确认现状。"
RESUME_GAP_PREFIX = "距上次会话已超过"

# 注入文本各段抬头。
RESUME_SECTION_HEADER = "【上次会话恢复摘要】"
INDEX_SECTION_HEADER = "【长期记忆索引（相当于已读过）】"
