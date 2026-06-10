"""Agent Teams（#0016）固定值集中落此（spec 不写、checklist 即契约）：实验门控环境变量 /
目录约定 / 任务状态枚举 / 协议消息类型 / 注入块标签 / 文件锁阈值 / 工具名 / delegate 保留集 /
文案模板。改这些等于改契约，须同步 checklist。

仿 #0014 `coreagent/agents/constants.py` 集中范式。注意目录与环境变量前缀**精确**为
``CODEAGENT`` / ``~/.codeagent``（无 r，区别于代码库其余 ``coreagent`` 拼写）——这是 checklist
钉死的契约值，勿"顺手"改成 coreagent。
"""

from pathlib import Path

# ── 实验门控（T1）：单一环境变量控制整套团队能力，默认关闭 ─────────────────────────────
EXPERIMENTAL_ENV_VAR = "CODEAGENT_EXPERIMENTAL_AGENT_TEAMS"
# 关闭判定取值集（大小写不敏感）：值落入此集（或为空）→ 关闭；否则非空即开启。
GATING_FALSY = frozenset({"0", "false", "no", "off"})

# ── 持久化目录约定（T3/T4/T5）——精确 ~/.codeagent（无 r，见模块 docstring）─────────────
TEAMS_DIR = Path("~/.codeagent/teams").expanduser()    # 团队配置根：{team}/config.json
TASKS_DIR = Path("~/.codeagent/tasks").expanduser()    # 共享任务根：{team}/ 下每任务一文件
TEAM_CONFIG_FILENAME = "config.json"
# 邮箱落团队目录下（每收件人一文件）：~/.codeagent/teams/{team}/mailbox/{name}.json
MAILBOX_SUBDIR = "mailbox"

# ── 任务状态枚举（T4，精确）─────────────────────────────────────────────────────────
TASK_BLOCKED = "blocked"          # 依赖未全完成，不可领
TASK_PENDING = "pending"          # 可领（未阻塞、未被认领）
TASK_IN_PROGRESS = "in_progress"  # 已被某队员认领
TASK_DONE = "done"                # 已完成
TASK_STATUSES = (TASK_BLOCKED, TASK_PENDING, TASK_IN_PROGRESS, TASK_DONE)

# ── 任务字段名（T4，精确；kebab-case 对齐 #0014 风格的多词键）──────────────────────────
TASK_FIELD_ID = "id"
TASK_FIELD_TITLE = "title"
TASK_FIELD_DEPS = "deps"
TASK_FIELD_ASSIGNEE = "assignee"
TASK_FIELD_FILES = "files"
TASK_FIELD_STATUS = "status"
TASK_FIELD_CLAIMED_BY = "claimed-by"
TASK_FIELD_CREATED = "created"
TASK_FIELD_UPDATED = "updated"

# ── 成员花名册字段名（T3，精确）─────────────────────────────────────────────────────
MEMBER_FIELD_NAME = "name"
MEMBER_FIELD_AGENT_ID = "agent-id"
MEMBER_FIELD_AGENT_TYPE = "agent-type"
# 运行态字段（系统维护、用户不手改）。
MEMBER_FIELD_SESSION_ID = "session-id"
MEMBER_FIELD_PANE_ID = "pane-id"

# ── 消息字段名（T5，精确）────────────────────────────────────────────────────────────
MSG_FIELD_FROM = "from"
MSG_FIELD_BODY = "body"
MSG_FIELD_TIMESTAMP = "timestamp"
MSG_FIELD_READ = "read"
MSG_FIELD_SUMMARY = "summary"
MSG_FIELD_KIND = "kind"            # 结构化协议消息类型（普通消息为 None / 缺省）

# ── 协议消息 kind 取值（T7，精确）───────────────────────────────────────────────────
KIND_APPROVAL_REQUEST = "approval-request"
KIND_APPROVAL_REPLY = "approval-reply"
KIND_SHUTDOWN = "shutdown"
KIND_IDLE = "idle"
PROTOCOL_KINDS = (KIND_APPROVAL_REQUEST, KIND_APPROVAL_REPLY, KIND_SHUTDOWN, KIND_IDLE)

# 审批回复约定（T10）：approval-reply 正文为此令牌（大小写不敏感）→ 批准；否则视为驳回，正文即反馈。
APPROVAL_APPROVE_TOKEN = "approve"

# ── 每轮邮箱注入块标签（T8，精确；不进可缓存前缀，仿 <background-agent-results>）──────────
INBOX_TAG_OPEN = "<team-inbox>"
INBOX_TAG_CLOSE = "</team-inbox>"

# ── 文件锁阈值（T2，精确）────────────────────────────────────────────────────────────
LOCK_SUFFIX = ".lock"
LOCK_RETRY_INTERVAL = 0.05    # 重试间隔（秒）= 50ms
LOCK_TIMEOUT = 2.0            # 总等待上限（秒）
LOCK_STALE_SECONDS = 30.0    # 锁文件 mtime 超此值视为陈旧、可夺取

# ── 后端取值（T6，精确）─────────────────────────────────────────────────────────────
BACKEND_AUTO = "auto"
BACKEND_IN_PROCESS = "in-process"
BACKEND_SPLIT_PANE = "split-pane"
BACKENDS = (BACKEND_AUTO, BACKEND_IN_PROCESS, BACKEND_SPLIT_PANE)

# ── 团队工具名（T11，精确）──────────────────────────────────────────────────────────
# 协作工具（队员与 Lead 共享；队员始终可见，即使角色白名单受限）。
TOOL_TASK_CREATE = "team_task_create"
TOOL_TASK_UPDATE = "team_task_update"
TOOL_TASK_LIST = "team_task_list"
TOOL_TASK_CLAIM = "team_task_claim"
TOOL_SEND_MESSAGE = "send_message"
COLLAB_TOOLS = frozenset({
    TOOL_TASK_CREATE, TOOL_TASK_UPDATE, TOOL_TASK_LIST, TOOL_TASK_CLAIM, TOOL_SEND_MESSAGE,
})
# 管理工具（仅 Lead；队员不获，防嵌套）。
TOOL_CREATE_TEAM = "create_team"
TOOL_SPAWN_MEMBERS = "spawn_members"
TOOL_DISBAND_TEAM = "disband_team"
MGMT_TOOLS = frozenset({TOOL_CREATE_TEAM, TOOL_SPAWN_MEMBERS, TOOL_DISBAND_TEAM})
# 全部团队工具（可见性门控用）。
ALL_TEAM_TOOLS = COLLAB_TOOLS | MGMT_TOOLS

# ── delegate（协调）模式保留工具集（T12，精确）──────────────────────────────────────
# delegate 开启时 Lead 保留：读类 + shell + 团队工具，去掉写/编辑类（write_file/edit_file）。
DELEGATE_RETAINED_BASE = frozenset({"read_file", "glob", "grep", "run_command"})
# 状态栏 delegate 标记文案。
DELEGATE_MARKER = "delegate"

# ── 文案模板（checklist 钉死，须含这些关键串）───────────────────────────────────────
# 单团队约束（T3）：已有团队再建新团队的报错。
SINGLE_TEAM_ERROR = "已存在团队「{name}」，请先 disband 再新建"
# 后端硬失败 / 提示文案（T6）。
SPLITPANE_MISSING_ERROR = "split-pane 需要 tmux 或 iTerm2，不会降级到 in-process"
SPLITPANE_NOT_IMPLEMENTED_ERROR = "split-pane 窗格编排尚未实现，见后续 spec"
AUTO_FALLBACK_NOTICE = "检测到 tmux/iTerm2，但分屏后端本期未实现，回落 in-process"
# 恢复语义（T13）：给已不存在的队员发消息时提示重新 spawn。
RESPAWN_NOTICE = "队员「{name}」已不存在（会话恢复后 in-process 队员不复活），请重新 spawn"

# ── 后端检测环境变量信号（T6）───────────────────────────────────────────────────────
ENV_TMUX = "TMUX"
ENV_TERM_PROGRAM = "TERM_PROGRAM"
ENV_WT_SESSION = "WT_SESSION"
ENV_GHOSTTY_RESOURCES = "GHOSTTY_RESOURCES_DIR"
TERM_ITERM = "iTerm.app"
TERM_VSCODE = "vscode"
TERM_GHOSTTY = "ghostty"
