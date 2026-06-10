"""FastAPI app：**多会话持久化**的流式（SSE）Web 驱动（#0022，supersedes-in-part #0020/#0021）。

#0020/#0021 把 Web 跑成「单会话、内存映射、重启即失、所有会话共用一个固定 workspace、mode 每条
消息带」。本期（#0022）在其上引入**多会话**：会话可新建 / 切换 / 看历史 / 删除；每个会话记住自己的
mode 与绑定的 workspace；对话**持久化**到 SQLite，刷新页面后历史 / mode / workspace 关联都在；会话
之间互不串。再加「重新生成」与「编辑消息重发」两种破坏式对话编辑。

七个端点（最薄驱动，哑转发器：只转发、不重塑事件、不算 diff）：
- ``POST /conversations``：建会话（带 mode → 生成 id + 分配独立 workspace + save 空记录）。
- ``GET  /conversations``：列摘要（id / title / mode / updated_at，**不含** messages）。
- ``GET  /conversations/{id}``：取单会话全量（含结构化 messages，供前端重放）。
- ``DELETE /conversations/{id}``：删记录（仅删 DB，**不**删 workspace 目录）。
- ``POST /conversations/{id}/messages``：发消息（体**仅** content、**无 mode**）→ 流式 → save。
- ``POST /conversations/{id}/regenerate``：破坏式丢最后一轮 assistant、从最后一条用户消息重跑。
- ``POST /conversations/{id}/messages/{index}/edit``：破坏式截断目标用户消息及其后、以新内容重跑。

会话状态一律经 ``store`` 按 id 存取（每请求 load → 运行 → save）：**无**进程内 id→Conversation
内存映射，会话间无共享可变状态（隔离铁律）。存储读写只在轮次边界（流开始前 load、协作式收尾后
save），放 ``to_thread`` 不阻塞逐帧推送。

每会话独立 workspace：根目录下按 ``session_id`` 建子目录作该会话 code 工具的 cwd，会话间文件天然
隔离；chat 会话也分配（其 profile 不用）。无人值守，故 code profile 声明的审批工具**自动放行**。

取消协作式、不硬杀（沿用 #0021）：客户端断开 / 点停止 → 置取消令牌；引擎流式中途与工具间查令牌即停，
长命令经进程组 kill；**save 落在引擎协作式排空之后**（``pump`` 的 finally），确保历史配平（``tool_use``
数 == ``tool_result`` 数）才落盘——取消中途停也不留半个 assistant。

依赖方向：本模块 import #0019 入口（``engine.run_profile``）、profile、#0021 序列化（``web.sse``）与
本期存储（``web.store``）；引擎 / 入口 / profile / 工具 / sse / store **绝不**反向 import 本模块。
"""

import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from coreagent.config import load_config
from coreagent.conversation import Conversation
from coreagent.engine import run_profile
from coreagent.events import RunDone, RunError, ToolCall, ToolResultEvent
from coreagent.profiles import chat_profile, code_profile
from coreagent.providers import create_provider
from coreagent.tools import build_registry
from coreagent.tools.exec_policy import use_policy
from coreagent.web.approval import (
    DIFF_TOOLS,
    PendingApprovals,
    build_diff,
    make_confirm_cb,
    parse_decision,
    snapshot_file,
)
from coreagent.web.auth import (
    MIN_PASSWORD_LEN,
    SessionTokens,
    clear_session_cookie,
    hash_password,
    set_session_cookie,
    token_from_request,
    verify_password,
)
from coreagent.web.files import list_tree, read_within
from coreagent.web.obslog import configure_logging, log_event
from coreagent.web.render import render_assistant_messages
from coreagent.web.sandbox import bwrap_available, make_web_policy
from coreagent.web.sms import DevSmsSender, VerificationService
from coreagent.web.sse import WebFrame, event_to_sse, file_diff_frame, web_frame_to_sse
from coreagent.web.store import (
    SessionRecord,
    UsageRecord,
    UserRecord,
    build_store,
    derive_title,
    ensure_user_workspace,
    truncate_for_edit,
    truncate_for_regenerate,
)

logger = logging.getLogger(__name__)

# 手机号格式（钉死值）：^1[3-9]\d{9}$；非法 → HTTP 400「手机号格式不正确」。
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
PHONE_FORMAT_ERROR = "手机号格式不正确"
# 每用户在飞请求上限（钉死值）：超限 → HTTP 429。
DEFAULT_MAX_INFLIGHT = 2

# 客户端断开轮询间隔（秒）：与事件流是否在产出**无关**地主动探测断开——确保长命令静默期
# （如 sleep 60，期间无事件流出）也能感知断开 → 置取消令牌 → 工具进程组 kill。
_DISCONNECT_POLL = 0.25


def _default_workspace_root() -> Path:
    """每用户 / 每会话 workspace 的**总根目录**默认 ``/tmp/coreagent-web``，环境变量
    ``COREAGENT_WEB_WORKSPACE`` 可覆盖。#0025：根下按 ``<user_id>/<session_id>`` 分配，
    用户间 / 会话间文件天然隔离，且作沙箱 ``--tmpfs`` 覆盖根（跨用户零可见）。"""
    return Path(os.environ.get("COREAGENT_WEB_WORKSPACE", "/tmp/coreagent-web")).resolve()


class CreateConversationRequest(BaseModel):
    """建会话请求：mode 限定 chat|code（非法值由 pydantic 拒为 422），创建时绑定、之后固定。

    ``project_root``（#0023 → #0025 作废）：web 多用户下**不再接受任意 host 路径**——该字段**被忽略**，
    workspace 一律强制分配在每用户根 ``<root>/<user_id>/<session_id>``。保留字段仅为兼容旧前端调用。
    """

    mode: Literal["chat", "code"]
    project_root: str | None = None  # #0025：忽略（不绑定任意 host 路径）


class ApprovalRequest(BaseModel):
    """审批回传体（#0023）：四档决策字符串（once/session/persist/reject）。

    刻意用 ``str``（**非** Literal）：非法值由端点的 ``parse_decision`` 判为 400（而非 pydantic 422），
    与 checklist「非法决策值 → 400」对齐。
    """

    decision: str


class MessageRequest(BaseModel):
    """一条用户消息 / 编辑内容：**仅** content（去 mode——mode 是会话属性，不再每条带）。"""

    content: str = Field(..., description="用户输入的文本内容")


# ── 鉴权请求体（#0025）────────────────────────────────────────────────────────────

class SendCodeRequest(BaseModel):
    """发码请求：仅手机号。"""

    phone: str


class RegisterRequest(BaseModel):
    """注册请求：手机号 + 短信验证码 + 密码（≥8）。"""

    phone: str
    code: str
    password: str


class LoginRequest(BaseModel):
    """登录请求：手机号 + 密码。"""

    phone: str
    password: str


def _validate_phone(phone: str) -> None:
    """校验手机号格式；非法抛 ``HTTPException(400)``，文案精确为「手机号格式不正确」。"""
    if not isinstance(phone, str) or not _PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail=PHONE_FORMAT_ERROR)


# ── React 前端静态托管（#0026）：根路径返回 Vite 构建产物 index.html；assets 由 StaticFiles 挂载 ──
# 旧内联 INDEX_HTML（#0022 原生界面）已被 coreagent/web/frontend 的 React 应用取代（supersedes-in-part
# #0020/#0024 前端呈现层）。后端 REST / SSE / 审批 / 渲染契约全部保留复用，仅换呈现层。


def _frontend_dist() -> Path:
    """前端构建产物目录：默认 ``coreagent/web/frontend/dist``，``COREAGENT_WEB_DIST`` 可覆盖。"""
    override = os.environ.get("COREAGENT_WEB_DIST")
    if override:
        return Path(override).resolve()
    return (Path(__file__).parent / "frontend" / "dist").resolve()


# 前端未构建时的兜底页（仍含 React 挂载点 id="root"，并提示构建命令）。
_DIST_MISSING_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    '<title>CoreAgent</title></head><body><div id="root"></div>'
    '<p style="font-family:system-ui;padding:24px;color:#5c5a57">'
    "前端尚未构建：请在 <code>coreagent/web/frontend</code> "
    "执行 <code>npm install &amp;&amp; npm run build</code>"
    "（或设 <code>COREAGENT_WEB_DIST</code>）。</p>"
    "</body></html>"
)


def _index_html() -> str:
    """读取并返回构建产物 index.html（每请求读，便于重构建免重启）；缺失 → 兜底页。"""
    index = _frontend_dist() / "index.html"
    try:
        return index.read_text(encoding="utf-8")
    except OSError:
        return _DIST_MISSING_HTML


def build_app(
    provider,
    store=None,
    workspace_root: Path | str | None = None,
    approval: frozenset[str] | None = None,
    chat_provider=None,
    web_search: dict | None = None,
    sms: VerificationService | None = None,
    tokens: SessionTokens | None = None,
    dev_mode: bool = True,
    cookie_secure: bool = False,
    max_inflight: int = DEFAULT_MAX_INFLIGHT,
    sandbox: bool = True,
) -> FastAPI:
    """用给定 provider 装配**多用户**安全 app（#0025，便于测试注入假 provider / store / sms）。

    ``store``：会话 + 用户 + 用量存储（缺省 ``build_store()``，按 env 选 SQLite/Postgres）。
    ``workspace_root``：每用户 / 每会话 workspace 总根（缺省 ``COREAGENT_WEB_WORKSPACE``）；code 会话
    workspace 强制 ``<root>/<user_id>/<session_id>``，不再绑任意 host 路径（supersedes-in-part #0023）。
    ``approval``（#0023）：code profile 审批集覆盖。``chat_provider`` / ``web_search``（#0024）：chat
    专用 provider + 服务端 web_search 声明。

    ``sms``（#0025）：验证码服务（缺省 ``DevSmsSender`` 打桩）。``tokens``：会话 token 注册表
    （缺省内存）。``dev_mode``：dev 发码响应回带验证码字段（便于本地 / 测试取码）。``cookie_secure``：
    会话 Cookie 是否带 Secure（生产 https 置 True；本地 http 闭环 / 测试 False）。``max_inflight``：每
    用户在飞请求上限（超限 429）。``sandbox``：code 会话子进程是否套 OS 沙箱（缺省开；CLI 路径不经此）。

    所有会话端点经「当前用户」依赖鉴权 + 按归属过滤（跨用户按 404）；闭包自包含、可起多实例互不串扰。
    """
    app = FastAPI(
        title="CoreAgent Web",
        description="多用户 / 安全 / 可部署：手机号鉴权 + 用户隔离 + 执行级沙箱的流式 Web 驱动（#0025）",
    )
    registry = build_registry()
    store = store or build_store()
    ws_root = Path(workspace_root) if workspace_root is not None else _default_workspace_root()
    ws_root.mkdir(parents=True, exist_ok=True)  # 启动时确保 workspace 总根存在
    ws_root_str = str(ws_root.resolve())
    sms = sms or VerificationService(DevSmsSender())
    tokens = tokens or SessionTokens()
    # 每用户在飞请求计数（#0025 T8，build_app 作用域、单进程单事件循环无需锁）。
    inflight: dict[str, int] = {}
    # 后台协作式排空中的引擎 task：客户端断开时 Starlette 会**硬 cancel** 响应协程，shield 让引擎
    # task 不被硬传播取消、继续协作式收尾（killpg + tool_result 配平 + save）；此集合持有强引用防 GC。
    drains: set = set()
    # ── 人在回路审批状态（#0023，build_app 作用域、跨并发请求共享）─────────────────────
    pending = PendingApprovals()
    session_trust: dict[str, set[str]] = {}

    # ── 当前用户依赖（#0025）：读 Cookie → token → user_id；缺失/失效 → 401（不触 DB，不阻塞）──
    async def current_user_id(request: Request) -> str:
        uid = tokens.resolve(token_from_request(request))
        if uid is None:
            raise HTTPException(status_code=401, detail="未登录")
        return uid

    def _try_acquire(uid: str) -> bool:
        n = inflight.get(uid, 0)
        if n >= max_inflight:
            return False
        inflight[uid] = n + 1
        return True

    def _release(uid: str) -> None:
        n = inflight.get(uid, 0)
        if n <= 1:
            inflight.pop(uid, None)
        else:
            inflight[uid] = n - 1

    # ── React 前端静态托管（#0026）：assets/ 由 StaticFiles 挂载，根路径返回构建产物 index.html ──
    _assets_dir = _frontend_dist() / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _index_html()

    # ── 鉴权端点（#0025 T4）──────────────────────────────────────────────────────

    @app.post("/auth/send-code")
    async def send_code(req: SendCodeRequest) -> dict:
        _validate_phone(req.phone)  # 非法 → 400「手机号格式不正确」
        code, err = await asyncio.to_thread(sms.request_code, req.phone)
        if err is not None:
            # 限流（重发间隔 / 每日上限）→ 429。
            raise HTTPException(status_code=429, detail=err)
        log_event("auth_send_code", phone=req.phone, ok=True)
        resp: dict = {"sent": True}
        if dev_mode:
            resp["code"] = code  # dev 打桩：回带验证码（仅 dev，便于本地 / 测试取码）
        return resp

    @app.post("/auth/register")
    async def register(req: RegisterRequest) -> dict:
        _validate_phone(req.phone)
        if len(req.password) < MIN_PASSWORD_LEN:
            raise HTTPException(status_code=400, detail=f"密码至少 {MIN_PASSWORD_LEN} 位")
        ok, err = sms.verify(req.phone, req.code)
        if not ok:
            raise HTTPException(status_code=400, detail=err or "验证码校验失败")
        existing = await asyncio.to_thread(store.get_user_by_phone, req.phone)
        if existing is not None:
            raise HTTPException(status_code=400, detail="该手机号已注册")
        user = UserRecord(
            id=uuid.uuid4().hex, phone=req.phone, password_hash=hash_password(req.password)
        )
        await asyncio.to_thread(store.create_user, user)
        log_event("auth_register", user=user.id, phone=req.phone)
        return {"id": user.id}

    @app.post("/auth/login")
    async def login(req: LoginRequest, response: Response) -> dict:
        user = await asyncio.to_thread(store.get_user_by_phone, req.phone)
        # 用户不存在 / 密码不匹配统一文案（不泄露手机号是否注册）。
        if user is None or not verify_password(user.password_hash, req.password):
            raise HTTPException(status_code=401, detail="手机号或密码不正确")
        token = tokens.issue(user.id)
        set_session_cookie(response, token, secure=cookie_secure)
        log_event("auth_login", user=user.id)
        return {"id": user.id}

    @app.post("/auth/logout")
    async def logout(request: Request, response: Response) -> dict:
        tokens.revoke(token_from_request(request))
        clear_session_cookie(response)
        return {"ok": True}

    @app.get("/auth/me")
    async def whoami(uid: str = Depends(current_user_id)) -> dict:
        return {"id": uid}

    # ── 会话 CRUD（按归属用户隔离，#0025 T5）──────────────────────────────────────

    @app.post("/conversations")
    async def create_conversation(
        req: CreateConversationRequest, uid: str = Depends(current_user_id)
    ) -> dict:
        # workspace 强制每用户根 / 会话（#0025）：code / chat 一视同仁分配 <root>/<uid>/<sid>；
        # project_root 字段被忽略（不再绑任意 host 路径）。
        session_id = uuid.uuid4().hex
        workspace = str(await asyncio.to_thread(ensure_user_workspace, ws_root, uid, session_id))
        record = SessionRecord(
            id=session_id, mode=req.mode, workspace=workspace, title="", owner_id=uid, messages=[]
        )
        await asyncio.to_thread(store.save, record)
        log_event("session_create", user=uid, session=session_id, mode=req.mode)
        return {"id": session_id, "mode": req.mode, "workspace": workspace}

    @app.get("/conversations")
    async def list_conversations(uid: str = Depends(current_user_id)) -> list[dict]:
        # 仅本人会话摘要（不含 messages）；按 updated_at 倒序由 store 保证。
        summaries = await asyncio.to_thread(store.list, uid)
        return [
            {"id": s.id, "title": s.title, "mode": s.mode, "updated_at": s.updated_at}
            for s in summaries
        ]

    @app.get("/conversations/{id}")
    async def get_conversation(id: str, uid: str = Depends(current_user_id)) -> dict:  # noqa: A002
        record = await _load_or_404(id, uid)
        # 全量含结构化 messages（供前端重放）；为每个 assistant 文本块附渲染 HTML（#0024 T2）。
        messages = await asyncio.to_thread(render_assistant_messages, record.messages)
        return {
            "id": record.id,
            "mode": record.mode,
            "workspace": record.workspace,
            "messages": messages,
        }

    @app.delete("/conversations/{id}")
    async def delete_conversation(id: str, uid: str = Depends(current_user_id)) -> dict:  # noqa: A002
        await _load_or_404(id, uid)  # 他人 / 不存在 → 404（不泄露存在性）
        await asyncio.to_thread(store.delete, id, uid)
        return {"id": id, "deleted": True}

    # ── 公共流式驱动（T5/T6/T7 共用，避免复制）────────────────────────────────────

    def _drive(
        record: SessionRecord, conversation: Conversation, request: Request, uid: str
    ) -> StreamingResponse:
        """按会话绑定的 mode/workspace 选 profile，流式跑 ``run_profile``；**协作式收尾后** save 回 store。

        ``conversation`` 须已按各端点规则准备好（发消息=追加新 user；重生成=截断；编辑=截断+追加）。
        save 落在 ``pump`` 的 finally（引擎 async-for 退出后）——此刻历史已配平（``tool_use`` 数 ==
        ``tool_result`` 数，取消中途停也无半个 assistant），且 ``pump`` 被 shield 保护必跑到底。

        **人在回路审批（#0023）**：code 会话的 confirm 不再自动放行，而是 per-会话交互确认回调——
        命中信任集直放，否则推审批帧 + 三路竞速等回传/超时/取消（见 ``web.approval``）。写/改类工具
        在 ToolCall 边界抓 before 快照、ToolResult 成功后读 after，推一帧真实 diff。
        """
        # 按 mode 选 profile + provider（#0024）：code 恒用主 provider + code_profile；chat 用
        # chat_provider（缺省回落主 provider）+ 接 web_search 声明的 chat_profile。两路解耦。
        if record.mode == "code":
            profile = code_profile(record.workspace, approval=approval)
            active_provider = provider
            # 执行级策略（#0025）：code 会话工具关进本会话 workspace——文件类约束子树 + 子进程 OS 沙箱
            # （断网/只读/清密钥/rlimit），不可用即 fail-closed。chat 无工具 → 不设策略。
            policy = make_web_policy(record.workspace, ws_root_str) if sandbox else None
        else:
            profile = chat_profile(web_search=web_search)
            active_provider = chat_provider or provider
            policy = None
        cancel = asyncio.Event()
        trust = session_trust.setdefault(record.id, set())  # 本会话内存信任集（跨多轮共享）
        sandbox_state = "off" if policy is None else ("on" if bwrap_available() else "fail-closed")
        log_event(
            "run_start", user=uid, session=record.id, mode=record.mode, sandbox=sandbox_state
        )

        async def event_stream():
            queue: asyncio.Queue = asyncio.Queue()
            _END = object()  # 引擎跑完（含 save 完成）哨兵
            # 写/改类 diff 的 before 快照：键 tool_id → (path, before 内容)；ToolCall 边界抓、ToolResult 消费。
            before_snapshots: dict[str, tuple[str, str | None]] = {}

            confirm = make_confirm_cb(
                session_id=record.id,
                queue=queue,
                pending=pending,
                session_trust=trust,
                persist_approved=record.persist_approved,
                cancel=cancel,
            )

            usage = {"in": 0, "out": 0, "tools": 0}  # 本轮用量累计（#0025 T9，pump 内更新、finally 落库）

            async def pump() -> None:
                # 引擎跑成独立任务：原样 async for 消费 #0019 入口事件喂队列（哑转发）；入口级异常兜底成
                # 一帧 RunError。**finally 里 save + 落用量 + 释放在飞**：引擎协作式收尾后历史已配平 →
                # 存回 store；pump 被消费侧 shield 保护必跑到底，故落盘 / 计量 / 释放不会被客户端断开掐断。
                # 工具执行经 ``use_policy``：code 会话套执行级策略（路径约束 + OS 沙箱），chat 无策略。
                try:
                    with use_policy(policy):
                        async for event in run_profile(
                            active_provider, profile, conversation, registry,
                            confirm=confirm, cancel=cancel,
                        ):
                            # diff before 快照：ToolCall 在引擎 confirm/执行**之前** emit，此刻文件未改 →
                            # 抓 before（仅写/改类）。引擎此时挂在 `yield ToolCall`，快照早于执行。
                            if isinstance(event, ToolCall):
                                usage["tools"] += 1  # 工具调用数（按发起计）
                                name = event.call.get("name", "")
                                if name in DIFF_TOOLS:
                                    tid = event.call.get("id", "")
                                    tpath = (event.call.get("input") or {}).get("path", "")
                                    before_snapshots[tid] = (
                                        tpath, snapshot_file(record.workspace, tpath)
                                    )
                            if isinstance(event, RunDone):
                                usage["in"] = event.input_tokens
                                usage["out"] = event.output_tokens
                            await queue.put(event)
                            # diff after：写/改类工具**成功**后读 after，与 before 比成一帧 file_diff。
                            if isinstance(event, ToolResultEvent):
                                tid = event.call.get("id", "")
                                snap = before_snapshots.pop(tid, None)
                                if snap is not None and not event.is_error:
                                    tpath, before = snap
                                    after = snapshot_file(record.workspace, tpath)
                                    await queue.put(
                                        file_diff_frame(tid, tpath, build_diff(tpath, before, after))
                                    )
                except Exception as exc:  # noqa: BLE001 —— 入口级异常转一帧错误，不让流裸崩
                    await queue.put(RunError(error_type=type(exc).__name__))
                finally:
                    record.messages = list(conversation.messages)
                    record.title = derive_title(conversation.messages)
                    try:
                        await asyncio.to_thread(store.save, record)
                    except Exception:  # noqa: BLE001 —— 存储失败不拖垮流收尾，仅记日志
                        logger.exception("会话存储失败：%s", record.id)
                    # 用量落库（#0025 T9）：每轮一条，归属用户 + 输入/输出 token + 工具调用数。
                    try:
                        await asyncio.to_thread(
                            store.add_usage,
                            UsageRecord(
                                id=uuid.uuid4().hex, owner_id=uid, session_id=record.id,
                                input_tokens=usage["in"], output_tokens=usage["out"],
                                tool_calls=usage["tools"],
                            ),
                        )
                    except Exception:  # noqa: BLE001 —— 计量失败不拖垮流收尾
                        logger.exception("用量落库失败：%s", record.id)
                    log_event(
                        "run_done", user=uid, session=record.id,
                        input_tokens=usage["in"], output_tokens=usage["out"], tool_calls=usage["tools"],
                    )
                    _release(uid)  # 释放在飞计数（无论正常 / 取消 / 异常）
                    # 终态清理：运行进入任一终态即移除该会话名下全部待决信号——之后对这些 tool_id 的回传
                    # 一律按过期 404（断线既不挂起、也不静默丢失 pending 审批）。
                    pending.clear_session(record.id)
                    await queue.put(_END)

            async def watch_disconnect() -> None:
                # 主动轮询客户端断开（不依赖事件是否在产出）：断开即置取消令牌（协作式，**不**对引擎
                # task 硬 asyncio cancel）。长命令静默期也能即时取消的关键。
                try:
                    while not cancel.is_set():
                        if await request.is_disconnected():
                            cancel.set()
                            return
                        await asyncio.sleep(_DISCONNECT_POLL)
                except Exception:  # noqa: BLE001 —— 断开探测异常不拖垮主流
                    pass

            engine_task = asyncio.ensure_future(pump())
            watch_task = asyncio.ensure_future(watch_disconnect())
            try:
                while True:
                    event = await queue.get()
                    if event is _END:
                        break
                    # 队列兼容承载两类：#0018 引擎事件（event_to_sse）与 web 层审批/diff 帧
                    # （WebFrame → web_frame_to_sse）；按类型分派序列化。
                    if isinstance(event, WebFrame):
                        yield web_frame_to_sse(event)
                    else:
                        yield event_to_sse(event)  # 一事件一帧（哑转发器）
            finally:
                # 收尾：正常结束 / 客户端断开 / 异常，都先置协作式取消令牌（同步、先于任何 await）。
                cancel.set()
                watch_task.cancel()
                # **等引擎 task 协作式排空 + save 完成**。shield：客户端断开时 Starlette 会硬 cancel 本
                # 响应协程，若直接 await/gather 引擎 task，该取消会硬传播进引擎（中断 to_thread → 跳过
                # add_tool_results → 历史失衡 + 漏 save）。shield 屏蔽硬传播：引擎只认协作令牌、自行收尾。
                try:
                    await asyncio.shield(engine_task)
                except asyncio.CancelledError:
                    # 外层被硬 cancel：引擎经 shield 仍在后台协作排空 + save，挂后台集合防 GC、跑完即移除；
                    # 尊重外层取消（响应协程本就该随断开结束），引擎在后台干净收尾并落盘。
                    drains.add(engine_task)
                    engine_task.add_done_callback(drains.discard)
                    raise
                finally:
                    drains.add(watch_task)
                    watch_task.add_done_callback(drains.discard)

        # SSE 响应：content-type 精确 text/event-stream；禁缓存 / 禁代理缓冲，保证逐帧实时下发。
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _load_or_404(id: str, uid: str) -> SessionRecord:  # noqa: A002
        # 归属过滤（#0025）：他人 / 不存在一律 404（不泄露存在性）。
        record = await asyncio.to_thread(store.load, id, uid)
        if record is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return record

    def _gate_inflight(uid: str) -> None:
        # 每用户在飞上限（#0025 T8）：超限快速拒绝 429；释放在 pump 收尾。
        if not _try_acquire(uid):
            raise HTTPException(status_code=429, detail="并发请求过多，请稍后再试")

    # ── 发消息（T5）：体仅 content、按会话存取 ───────────────────────────────────────

    @app.post("/conversations/{id}/messages")
    async def post_message(
        id: str, req: MessageRequest, request: Request, uid: str = Depends(current_user_id)  # noqa: A002
    ) -> StreamingResponse:
        record = await _load_or_404(id, uid)
        _gate_inflight(uid)
        conversation = Conversation()
        conversation.messages = list(record.messages)
        conversation.add_user(req.content)  # 追加新一轮用户消息（复用 Conversation 形态）
        return _drive(record, conversation, request, uid)

    # ── 重新生成（T6）：破坏式丢最后一轮 assistant、从最后一条用户消息重跑 ───────────────

    @app.post("/conversations/{id}/regenerate")
    async def regenerate(
        id: str, request: Request, uid: str = Depends(current_user_id)  # noqa: A002
    ) -> StreamingResponse:
        record = await _load_or_404(id, uid)
        try:
            truncated = truncate_for_regenerate(record.messages)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        _gate_inflight(uid)
        conversation = Conversation()
        conversation.messages = truncated
        return _drive(record, conversation, request, uid)

    # ── 编辑重发（T7）：破坏式截断目标用户消息及其后、以新内容重跑 ─────────────────────

    @app.post("/conversations/{id}/messages/{index}/edit")
    async def edit_message(
        id: str, index: int, req: MessageRequest, request: Request,  # noqa: A002
        uid: str = Depends(current_user_id),
    ) -> StreamingResponse:
        record = await _load_or_404(id, uid)
        try:
            truncated = truncate_for_edit(record.messages, index)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        _gate_inflight(uid)
        conversation = Conversation()
        conversation.messages = truncated
        conversation.add_user(req.content)  # 以新内容作新一轮用户消息
        return _drive(record, conversation, request, uid)

    # ── 审批回传端点（#0023 T5）：按「会话 + 工具调用标识」定位待决信号、送回四档决策 ──────────
    @app.post("/conversations/{id}/approvals/{tool_id}")
    async def approve(
        id: str, tool_id: str, req: ApprovalRequest, uid: str = Depends(current_user_id)  # noqa: A002
    ) -> dict:
        # 归属校验（#0025 审批隔离）：他人会话 → 404，绝不 resolve 到他人待决 Future。
        await _load_or_404(id, uid)
        # 非法决策值（非四档）→ 400（刻意不由 pydantic 422，与 checklist 对齐）。
        decision = parse_decision(req.decision)
        if decision is None:
            raise HTTPException(status_code=400, detail=f"非法决策值：{req.decision}")
        # 未知 / 已过期（运行终态后已被 clear_session 清理）标识 → 404。
        if not pending.resolve(id, tool_id, decision):
            raise HTTPException(status_code=404, detail="未知或已过期的审批请求")
        return {"id": id, "tool_id": tool_id, "decision": decision.value}

    # ── 文件浏览端点（#0023 T8 → #0025 归属隔离）：列目录树 / 读文件，限定在本人会话 workspace 内 ──
    @app.get("/conversations/{id}/files")
    async def list_files(id: str, uid: str = Depends(current_user_id)) -> dict:  # noqa: A002
        record = await _load_or_404(id, uid)
        tree = await asyncio.to_thread(list_tree, record.workspace)
        return {"root": record.workspace, "tree": tree}

    @app.get("/conversations/{id}/files/content")
    async def file_content(id: str, path: str, uid: str = Depends(current_user_id)) -> dict:  # noqa: A002
        record = await _load_or_404(id, uid)
        status, text = await asyncio.to_thread(read_within, record.workspace, path)
        if status == "denied":
            # 越界（含 `..` 跳出项目根 / 符号链接逃逸）→ 拒绝，绝不读根外文件。
            raise HTTPException(status_code=400, detail="路径越界：拒绝访问项目根外文件")
        if status == "missing":
            raise HTTPException(status_code=404, detail="文件不存在")
        return {"path": path, "content": text}

    # ── 用量端点（#0025 T9）：只读，返回**本人**聚合用量（总输入/输出 token + 请求数）──────────
    @app.get("/usage")
    async def usage_summary(uid: str = Depends(current_user_id)) -> dict:
        summary = await asyncio.to_thread(store.usage_summary, uid)
        return {
            "input_tokens": summary.input_tokens,
            "output_tokens": summary.output_tokens,
            "requests": summary.requests,
        }

    return app


def _chat_provider_config(config):
    """据 ``config.web.chat`` 构造 chat 独立 provider 的配置：空字段回落主配置（#0024）。

    chat 的 ``protocol`` / ``api_key`` / ``base_url`` 留空时沿用主配置（最小配置 = 只设 ``model``
    即可在主端点上换模型）；其余字段（thinking / max_tokens / context_window 等）随主配置 copy。
    """
    chat = config.web.chat
    return config.model_copy(update={
        "protocol": chat.protocol or config.protocol,
        "model": chat.model or config.model,
        "api_key": chat.api_key or config.api_key,
        "base_url": chat.base_url or config.base_url,
    })


def create_app(config_path: str = "config.yaml") -> FastAPI:
    """生产装配（#0025）：按 config.yaml 建 provider（同 CLI，走 DeepSeek 的 Anthropic 端点）再装
    **多用户**安全 app。

    store 走 ``build_store()``（``COREAGENT_WEB_DB_URL`` 以 postgres 开头 → Postgres，否则 SQLite）；
    workspace 总根读 ``COREAGENT_WEB_WORKSPACE``；审批集覆盖来自 ``config.web``。短信走 dev 打桩
    （真 provider 配密钥后在 ``sms.py`` 接入）。``COREAGENT_WEB_DEV=0`` 关 dev 发码回带；
    ``COREAGENT_COOKIE_SECURE=1`` 让会话 Cookie 带 Secure（https 部署）。

    chat 独立 provider + web_search（#0024）来自 ``config.web.chat``（与 code 解耦，逻辑不变）。
    """
    configure_logging()  # 结构化 JSON 日志（#0025 T10）
    config = load_config(config_path)
    provider = create_provider(config)
    approval_override = (
        frozenset(config.web.approval) if config.web.approval is not None else None
    )
    chat_cfg = config.web.chat
    # chat provider：配了独立 model → 建独立 provider；否则 None（chat 复用主 provider）。
    chat_provider = create_provider(_chat_provider_config(config)) if chat_cfg.model else None
    # web_search 声明：启用 → 服务端工具声明 dict（无 input_schema）；否则 None（chat 纯问答）。
    web_search = (
        {
            "type": chat_cfg.web_search.version,
            "name": "web_search",
            "max_uses": chat_cfg.web_search.max_uses,
        }
        if chat_cfg.web_search.enabled
        else None
    )
    return build_app(
        provider,
        approval=approval_override,
        chat_provider=chat_provider,
        web_search=web_search,
        dev_mode=os.environ.get("COREAGENT_WEB_DEV", "1") != "0",
        cookie_secure=os.environ.get("COREAGENT_COOKIE_SECURE", "0") == "1",
    )
