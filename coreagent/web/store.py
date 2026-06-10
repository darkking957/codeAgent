"""会话存储 + 用户 / 用量 + 会话编辑纯函数 + 每用户/每会话 workspace 分配（#0022 + #0025）。

#0022 把「单会话、内存映射、重启即失」升级为**多会话持久化**；#0025 在其上引入**多用户**：

- **存储抽象**（``SessionStore``）：会话三操作（save / load / list / delete，均可按**归属用户**过滤）
  + 用户三操作（create_user / get_user / get_user_by_phone）+ 用量两操作（add_usage / usage_summary）。
  两实现：``SQLiteSessionStore``（默认 / 测试 / 本地闭环，零外部依赖）与 ``PostgresStore``（部署，
  psycopg 同步驱动 + app 层 ``to_thread`` 线程桥，不阻塞事件循环）。``build_store`` 据 env 选后端。
- **数据结构**：``SessionRecord``（含 ``owner_id`` 归属用户）/ ``SessionSummary`` / ``UserRecord``
  （手机号 + 密码哈希）/ ``UsageRecord``（每轮一条：输入/输出 token + 工具调用数 + 时间）。
- **会话编辑纯函数**：标题派生 / 重生成截断 / 编辑截断（「用户撰写消息」判定不变）。
- **workspace 分配**：``<root>/<user_id>/<session_id>``（#0025），用户间 / 会话间文件天然隔离。

单向依赖铁律：本模块只依赖标准库（``sqlite3`` / ``json`` / ``dataclasses`` / ``pathlib``），
Postgres 后端的 ``psycopg`` **延迟 import**（仅 ``PostgresStore`` 实例化时）；**绝不**被引擎 / 入口 /
profile / 工具反向 import（DB 库只活在 web 层）。messages 直接是 ``Conversation.messages`` 的形态。
"""

import json
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

# 标题截断长度（首条用户撰写消息取前 N 字符）。
TITLE_MAX = 40


# ════════════════════════════════════════════════════════════════════════════════
# 数据结构：全量记录 / 轻量摘要
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class SessionRecord:
    """一个会话的全量记录：id + 绑定的 mode/workspace + 标题 + 全量 messages + 时间戳。

    ``messages`` 直接是 ``Conversation.messages`` 的形态（含 ``tool_use`` / ``tool_result`` 块），
    save 时 JSON 序列化进单列、load 时还原。

    ``persist_approved``（#0023）：本会话「永久允许」过的工具名集合（粗粒度，按工具名）。落 SQLite
    单列、跨重启随会话回来；作用域**仅本会话**（换会话仍需审批）。旧库无该列时 load 平滑回落空集。
    """

    id: str
    mode: str
    workspace: str
    title: str
    messages: list[dict] = field(default_factory=list)
    persist_approved: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    # 归属用户（#0025）：会话归属到登录用户 id；列表 / 读取 / 删除 / 保存全部按此过滤（跨用户零可见）。
    # 缺省空串：未登录 / 旧库回落（旧库无该列时 load 平滑回落 ""）；多用户路径下端点必填真实 user_id。
    owner_id: str = ""


@dataclass
class SessionSummary:
    """会话摘要（``list`` 专用）：仅 id / title / mode / updated_at，**不含** messages。"""

    id: str
    title: str
    mode: str
    updated_at: float


@dataclass
class UserRecord:
    """一个用户：id + 手机号（唯一）+ 密码哈希（argon2id；**绝不**存明文）+ 注册时间（#0025）。"""

    id: str
    phone: str
    password_hash: str
    created_at: float = 0.0


@dataclass
class UsageRecord:
    """一条用量事件（#0025）：每轮对话落一条，归属用户 + 输入/输出 token + 工具调用数 + 时间。"""

    id: str
    owner_id: str
    session_id: str
    input_tokens: int
    output_tokens: int
    tool_calls: int
    created_at: float = 0.0


@dataclass
class UsageSummary:
    """本人用量聚合（``/usage`` 端点返回）：累计输入/输出 token + 请求数（用量事件条数）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0


# ════════════════════════════════════════════════════════════════════════════════
# 存储接口 + SQLite 实现
# ════════════════════════════════════════════════════════════════════════════════

class SessionStore(ABC):
    """会话 + 用户 + 用量存储抽象（#0022/#0025）。

    会话三操作均带可选 ``owner_id`` 归属过滤（None = 不过滤，向后兼容 #0022 既有调用 / 单用户路径；
    多用户路径下端点一律传真实 user_id → 跨用户按「不存在」处理）。两实现：``SQLiteSessionStore``
    与 ``PostgresStore``。会话状态一律经此按 id 存取，严禁用进程级全局变量存对话状态。
    """

    # ── 会话（带归属过滤）─────────────────────────────────────────────────────────
    @abstractmethod
    def save(self, record: SessionRecord) -> None:
        """按 id upsert 整条记录（含 messages 全量块 + ``owner_id``）；同时刷新 updated_at。"""

    @abstractmethod
    def load(self, session_id: str, owner_id: str | None = None) -> SessionRecord | None:
        """按 id 取全量记录；``owner_id`` 非 None 时**另要求归属匹配**——不匹配返回 None（按不存在）。"""

    @abstractmethod
    def list(self, owner_id: str | None = None) -> list[SessionSummary]:
        """列会话摘要（不含 messages），按 updated_at 倒序；``owner_id`` 非 None 时只列归属该用户的。"""

    @abstractmethod
    def delete(self, session_id: str, owner_id: str | None = None) -> None:
        """按 id 删记录（仅删 DB 记录，不动 workspace 目录）；``owner_id`` 非 None 时只删归属匹配的。"""

    # ── 用户（#0025）────────────────────────────────────────────────────────────
    @abstractmethod
    def create_user(self, record: UserRecord) -> None:
        """插入用户；手机号唯一冲突应抛异常（端点转「已注册」）。"""

    @abstractmethod
    def get_user(self, user_id: str) -> UserRecord | None:
        """按 id 取用户；不存在返回 None。"""

    @abstractmethod
    def get_user_by_phone(self, phone: str) -> UserRecord | None:
        """按手机号取用户；不存在返回 None（注册查重 / 登录均用）。"""

    # ── 用量（#0025）────────────────────────────────────────────────────────────
    @abstractmethod
    def add_usage(self, record: UsageRecord) -> None:
        """追加一条用量事件（每轮对话一条）。"""

    @abstractmethod
    def usage_summary(self, owner_id: str) -> UsageSummary:
        """聚合**本人**用量：累计输入/输出 token + 请求数（只看归属 ``owner_id`` 的用量事件）。"""


def _default_db_path() -> Path:
    """SQLite DB 默认路径 ``~/.config/coreagent/sessions.db``，环境变量 ``COREAGENT_WEB_DB`` 可覆盖。"""
    raw = os.environ.get("COREAGENT_WEB_DB", "~/.config/coreagent/sessions.db")
    return Path(raw).expanduser()


class SQLiteSessionStore(SessionStore):
    """SQLite 单文件实现：表 ``sessions``，messages 列存 JSON 文本。

    最简方案：无内存缓存、无索引调优、无连接池——每次操作开一个短连接（``sqlite3`` 连接不跨线程
    共享，故 ``to_thread`` 调用也安全）。构造时建库目录 + 建表（``IF NOT EXISTS``，幂等）。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id         TEXT PRIMARY KEY,
                    mode       TEXT NOT NULL,
                    workspace  TEXT NOT NULL,
                    title      TEXT NOT NULL,
                    messages   TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            # 迁移容错（#0023/#0025）：旧库缺列时补建；已存在则 ALTER 抛 OperationalError，吞掉即幂等。
            for ddl in (
                "ALTER TABLE sessions ADD COLUMN persist_approved TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE sessions ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass
            # 用户表（#0025）：手机号唯一；password_hash 存 argon2id 串（绝不存明文）。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            TEXT PRIMARY KEY,
                    phone         TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at    REAL NOT NULL
                )
                """
            )
            # 用量表（#0025）：每轮一条事件，按 owner_id 聚合。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    id            TEXT PRIMARY KEY,
                    owner_id      TEXT NOT NULL,
                    session_id    TEXT NOT NULL,
                    input_tokens  INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    tool_calls    INTEGER NOT NULL,
                    created_at    REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_owner ON usage(owner_id)")

    def save(self, record: SessionRecord) -> None:
        # updated_at 由存储统一打点（每次 save 必刷新）；created_at 取记录值（首次插入定、之后不动）。
        record.updated_at = time.time()
        if not record.created_at:
            record.created_at = record.updated_at
        payload = json.dumps(record.messages, ensure_ascii=False)
        persist = json.dumps(record.persist_approved, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, mode, workspace, title, messages, persist_approved, owner_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    mode             = excluded.mode,
                    workspace        = excluded.workspace,
                    title            = excluded.title,
                    messages         = excluded.messages,
                    persist_approved = excluded.persist_approved,
                    owner_id         = excluded.owner_id,
                    updated_at       = excluded.updated_at
                """,
                (
                    record.id,
                    record.mode,
                    record.workspace,
                    record.title,
                    payload,
                    persist,
                    record.owner_id,
                    record.created_at,
                    record.updated_at,
                ),
            )

    def load(self, session_id: str, owner_id: str | None = None) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        keys = row.keys()
        owner = row["owner_id"] if "owner_id" in keys else ""
        # 归属过滤（#0025）：要求归属匹配但不匹配 → 按不存在（不泄露存在性）。
        if owner_id is not None and owner != owner_id:
            return None
        # persist_approved 列在迁移后必存在；保险起见旧库/异常回落空集（不让缺列拖垮 load）。
        persist_raw = row["persist_approved"] if "persist_approved" in keys else "[]"
        try:
            persist = json.loads(persist_raw) if persist_raw else []
        except (TypeError, ValueError):
            persist = []
        return SessionRecord(
            id=row["id"],
            mode=row["mode"],
            workspace=row["workspace"],
            title=row["title"],
            messages=json.loads(row["messages"]),
            persist_approved=persist,
            owner_id=owner,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list(self, owner_id: str | None = None) -> list[SessionSummary]:
        sql = "SELECT id, title, mode, updated_at FROM sessions"
        params: tuple = ()
        if owner_id is not None:
            sql += " WHERE owner_id = ?"
            params = (owner_id,)
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            SessionSummary(
                id=r["id"], title=r["title"], mode=r["mode"], updated_at=r["updated_at"]
            )
            for r in rows
        ]

    def delete(self, session_id: str, owner_id: str | None = None) -> None:
        sql = "DELETE FROM sessions WHERE id = ?"
        params: tuple = (session_id,)
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params = (session_id, owner_id)
        with self._connect() as conn:
            conn.execute(sql, params)

    # ── 用户（#0025）────────────────────────────────────────────────────────────
    def create_user(self, record: UserRecord) -> None:
        if not record.created_at:
            record.created_at = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (id, phone, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (record.id, record.phone, record.password_hash, record.created_at),
            )

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row)

    def get_user_by_phone(self, phone: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        return self._row_to_user(row)

    @staticmethod
    def _row_to_user(row) -> UserRecord | None:
        if row is None:
            return None
        return UserRecord(
            id=row["id"],
            phone=row["phone"],
            password_hash=row["password_hash"],
            created_at=row["created_at"],
        )

    # ── 用量（#0025）────────────────────────────────────────────────────────────
    def add_usage(self, record: UsageRecord) -> None:
        if not record.created_at:
            record.created_at = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage
                    (id, owner_id, session_id, input_tokens, output_tokens, tool_calls, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.owner_id,
                    record.session_id,
                    record.input_tokens,
                    record.output_tokens,
                    record.tool_calls,
                    record.created_at,
                ),
            )

    def usage_summary(self, owner_id: str) -> UsageSummary:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0)  AS it,
                       COALESCE(SUM(output_tokens), 0) AS ot,
                       COUNT(*)                         AS n
                FROM usage WHERE owner_id = ?
                """,
                (owner_id,),
            ).fetchone()
        return UsageSummary(
            input_tokens=int(row["it"]), output_tokens=int(row["ot"]), requests=int(row["n"])
        )


# ════════════════════════════════════════════════════════════════════════════════
# 会话编辑纯函数（标题派生 / 重生成截断 / 编辑截断）
# ════════════════════════════════════════════════════════════════════════════════

def is_user_authored(message: dict) -> bool:
    """用户撰写消息判定（重生成 / 编辑 / 标题三处共用）。

    判定式精确为「``role == "user"`` 且 ``content`` 是纯文本字符串」。承载 ``tool_result`` 的
    user 角色消息（content 为块列表）**不**算用户撰写——二者 role 都是 user，唯靠 content 形态区分。
    """
    return message.get("role") == "user" and isinstance(message.get("content"), str)


def derive_title(messages: list[dict]) -> str:
    """从**首条用户撰写消息**内容派生标题（截断至 ``TITLE_MAX`` 字符）。

    无用户撰写消息（空历史 / 首条恰为 tool_result）→ 返回空串（前端显示「新会话」）。
    """
    for m in messages:
        if is_user_authored(m):
            return m["content"][:TITLE_MAX]
    return ""


def _last_user_authored_index(messages: list[dict]) -> int | None:
    """最后一条用户撰写消息的下标；无则 None。"""
    for i in range(len(messages) - 1, -1, -1):
        if is_user_authored(messages[i]):
            return i
    return None


def truncate_for_regenerate(messages: list[dict]) -> list[dict]:
    """重生成截断：保留到**最后一条用户撰写消息**（含），丢弃其后全部。

    即丢掉最后一轮 assistant（含其 ``tool_use``）及夹在其间承载 ``tool_result`` 的 user 消息，
    停在最后一条用户撰写消息上，供从该处重跑。无用户撰写消息 → 抛 ``ValueError``（空历史不能重生成）。
    """
    last = _last_user_authored_index(messages)
    if last is None:
        raise ValueError("无用户撰写消息，无法重生成")
    return list(messages[: last + 1])


def truncate_for_edit(messages: list[dict], index: int) -> list[dict]:
    """编辑截断：截到 ``[0, index)``，丢弃目标用户消息及其后全部（供追加新内容重跑）。

    ``index`` 必须指向一条**用户撰写消息**，否则抛 ``ValueError``：
    越界（< 0 或 ≥ 长度）或目标非用户撰写（如 tool_result 承载消息 / assistant 消息）。
    """
    if index < 0 or index >= len(messages):
        raise ValueError(f"编辑 index 越界：{index}")
    if not is_user_authored(messages[index]):
        raise ValueError(f"编辑目标非用户撰写消息：index={index}")
    return list(messages[:index])


# ════════════════════════════════════════════════════════════════════════════════
# 每会话 workspace 分配
# ════════════════════════════════════════════════════════════════════════════════

def ensure_workspace(root: Path | str, session_id: str) -> Path:
    """在根目录下按 ``session_id`` 建子目录作该会话 code 工具的 cwd，确保存在并返回路径。

    ``<root>/<session_id>`` 即该会话 workspace；会话间文件天然隔离。已存在则幂等。
    """
    ws = Path(root) / session_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def ensure_user_workspace(root: Path | str, user_id: str, session_id: str) -> Path:
    """每用户根 / 会话子目录分配（#0025）：``<root>/<user_id>/<session_id>``。

    用户间（不同 ``user_id``）与会话间（不同 ``session_id``）文件天然隔离；已存在则幂等。
    返回**已 resolve** 的绝对路径——作 code 工具 cwd 兼路径约束根（越界判定以此为子树边界）。
    """
    ws = (Path(root) / user_id / session_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws.resolve()


# ════════════════════════════════════════════════════════════════════════════════
# Postgres 后端（#0025）：部署用；psycopg 同步驱动 + app 层 to_thread 线程桥
# ════════════════════════════════════════════════════════════════════════════════

class PostgresStore(SessionStore):
    """Postgres 实现（``psycopg`` 同步 API，**延迟 import**）：与 SQLite 同语义、同接口。

    最简方案：每操作开一个短连接（``autocommit``），不引连接池 / ORM。``%s`` 占位符；归属过滤与
    用户 / 用量方法逐一对应 SQLite 版。建表幂等（``CREATE TABLE IF NOT EXISTS``），全新起不迁移旧库。

    异步不阻塞：本类**同步**，由 app 层与 SQLite 同范式经 ``asyncio.to_thread`` 调用（线程桥）。
    """

    def __init__(self, conninfo: str | None = None) -> None:
        import psycopg  # 延迟 import：未装 psycopg 也能 import 本模块（仅实例化 PostgresStore 才需要）

        self._psycopg = psycopg
        self._conninfo = conninfo or os.environ.get("COREAGENT_WEB_DB_URL", "")
        self._init_table()

    def _connect(self):
        return self._psycopg.connect(self._conninfo, autocommit=True)

    def _init_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id               TEXT PRIMARY KEY,
                    mode             TEXT NOT NULL,
                    workspace        TEXT NOT NULL,
                    title            TEXT NOT NULL,
                    messages         TEXT NOT NULL,
                    persist_approved TEXT NOT NULL DEFAULT '[]',
                    owner_id         TEXT NOT NULL DEFAULT '',
                    created_at       DOUBLE PRECISION NOT NULL,
                    updated_at       DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            TEXT PRIMARY KEY,
                    phone         TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at    DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    id            TEXT PRIMARY KEY,
                    owner_id      TEXT NOT NULL,
                    session_id    TEXT NOT NULL,
                    input_tokens  BIGINT NOT NULL,
                    output_tokens BIGINT NOT NULL,
                    tool_calls    BIGINT NOT NULL,
                    created_at    DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_owner ON usage(owner_id)")

    def save(self, record: SessionRecord) -> None:
        record.updated_at = time.time()
        if not record.created_at:
            record.created_at = record.updated_at
        payload = json.dumps(record.messages, ensure_ascii=False)
        persist = json.dumps(record.persist_approved, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, mode, workspace, title, messages, persist_approved, owner_id,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    mode             = EXCLUDED.mode,
                    workspace        = EXCLUDED.workspace,
                    title            = EXCLUDED.title,
                    messages         = EXCLUDED.messages,
                    persist_approved = EXCLUDED.persist_approved,
                    owner_id         = EXCLUDED.owner_id,
                    updated_at       = EXCLUDED.updated_at
                """,
                (
                    record.id, record.mode, record.workspace, record.title, payload,
                    persist, record.owner_id, record.created_at, record.updated_at,
                ),
            )

    def load(self, session_id: str, owner_id: str | None = None) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, mode, workspace, title, messages, persist_approved, owner_id,"
                " created_at, updated_at FROM sessions WHERE id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        owner = row[6] or ""
        if owner_id is not None and owner != owner_id:
            return None
        try:
            persist = json.loads(row[5]) if row[5] else []
        except (TypeError, ValueError):
            persist = []
        return SessionRecord(
            id=row[0], mode=row[1], workspace=row[2], title=row[3],
            messages=json.loads(row[4]), persist_approved=persist, owner_id=owner,
            created_at=row[7], updated_at=row[8],
        )

    def list(self, owner_id: str | None = None) -> list[SessionSummary]:
        sql = "SELECT id, title, mode, updated_at FROM sessions"
        params: tuple = ()
        if owner_id is not None:
            sql += " WHERE owner_id = %s"
            params = (owner_id,)
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [SessionSummary(id=r[0], title=r[1], mode=r[2], updated_at=r[3]) for r in rows]

    def delete(self, session_id: str, owner_id: str | None = None) -> None:
        sql = "DELETE FROM sessions WHERE id = %s"
        params: tuple = (session_id,)
        if owner_id is not None:
            sql += " AND owner_id = %s"
            params = (session_id, owner_id)
        with self._connect() as conn:
            conn.execute(sql, params)

    def create_user(self, record: UserRecord) -> None:
        if not record.created_at:
            record.created_at = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (id, phone, password_hash, created_at) VALUES (%s, %s, %s, %s)",
                (record.id, record.phone, record.password_hash, record.created_at),
            )

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, phone, password_hash, created_at FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        return UserRecord(id=row[0], phone=row[1], password_hash=row[2], created_at=row[3]) if row else None

    def get_user_by_phone(self, phone: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, phone, password_hash, created_at FROM users WHERE phone = %s", (phone,)
            ).fetchone()
        return UserRecord(id=row[0], phone=row[1], password_hash=row[2], created_at=row[3]) if row else None

    def add_usage(self, record: UsageRecord) -> None:
        if not record.created_at:
            record.created_at = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO usage (id, owner_id, session_id, input_tokens, output_tokens,"
                " tool_calls, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id, record.owner_id, record.session_id, record.input_tokens,
                    record.output_tokens, record.tool_calls, record.created_at,
                ),
            )

    def usage_summary(self, owner_id: str) -> UsageSummary:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COUNT(*)"
                " FROM usage WHERE owner_id = %s",
                (owner_id,),
            ).fetchone()
        return UsageSummary(input_tokens=int(row[0]), output_tokens=int(row[1]), requests=int(row[2]))


def build_store() -> SessionStore:
    """据 env 选后端（#0025）：``COREAGENT_WEB_DB_URL`` 以 ``postgres`` 开头 → ``PostgresStore``；
    否则 ``SQLiteSessionStore``（默认 / 测试 / 本地闭环，读 ``COREAGENT_WEB_DB``）。"""
    url = os.environ.get("COREAGENT_WEB_DB_URL", "")
    if url.startswith("postgres"):
        return PostgresStore(url)
    return SQLiteSessionStore()
