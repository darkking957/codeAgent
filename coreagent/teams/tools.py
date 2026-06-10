"""T11｜团队工具集（协作 + 管理）+ 队员工具可见性过滤。

工具基类仿 `coreagent/tools/base.py`（name/description/parameters/execute→ToolResult）；依赖经构造
注入（仿 #0012 ``LoadSkillTool(store)``）——这里统一注入 ``TeamManager``（duck-typed，避免循环导入）。

  - **协作工具**（队员与 Lead 共享、队员始终可见）：``team_task_create`` / ``team_task_update`` /
    ``team_task_list`` / ``team_task_claim`` / ``send_message``。
  - **管理工具**（仅 Lead）：``create_team`` / ``spawn_members`` / ``disband_team``。

工具可见性（``compute_member_tool_filter``）仿 #0014 三层过滤：①队员 allow 集**始终并入**协作工具
（即使角色白名单受限）；②**剔除**团队管理工具与 #0014 委派工具 ``spawn_agent``（防嵌套）；③白名单
裁剪仍走 registry。所有团队工具免确认（``requires_confirmation=False``）：队员用非交互门禁，需确认
工具会被 fail-closed 挡掉；且团队协调动作本身低危。
"""

import logging

from coreagent.agents.constants import DELEGATION_TOOL_NAME
from coreagent.agents.runtime import compute_tool_filter
from coreagent.teams import constants
from coreagent.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


# ── 队员工具可见性过滤（T11）────────────────────────────────────────────────────────
def compute_member_tool_filter(registry, role) -> set[str]:
    """队员可见工具名集合：#0014 隔离过滤 + 协作工具（始终并入）− 管理工具 − spawn_agent。"""
    allow = compute_tool_filter(registry, role, background=False)
    allow |= set(constants.COLLAB_TOOLS)       # 协作工具始终可用（即使角色白名单受限）
    allow -= set(constants.MGMT_TOOLS)         # 队员不获团队管理工具
    allow.discard(DELEGATION_TOOL_NAME)        # 队员不获 #0014 委派工具（防嵌套；HIDDEN 已剔，双保险）
    return allow


# ── delegate 协调模式工具收窄（T12）──────────────────────────────────────────────────
def delegate_retained_tools() -> set[str]:
    """delegate 开启时 Lead 的保留工具名集：读类 + shell + 全部团队工具（去掉写/编辑类）。"""
    return set(constants.DELEGATE_RETAINED_BASE) | set(constants.ALL_TEAM_TOOLS)


def compute_delegate_filter(registry) -> set[str]:
    """delegate 收窄过滤集 = registry 实有工具 ∩ 保留集（供 run_agent_turn 的 extra_tool_filter）。"""
    return set(registry.names()) & delegate_retained_tools()


# ── 工具基类（统一持 manager）─────────────────────────────────────────────────────
class _TeamTool(Tool):
    """团队工具基类：持 TeamManager 引用；团队工具一律免确认。"""

    requires_confirmation = False

    def __init__(self, manager) -> None:
        self._m = manager

    def _need_team(self) -> ToolResult | None:
        if self._m.team is None or self._m.task_store is None:
            return ToolResult.fail("当前没有团队（请先 create_team）")
        return None


# ── 协作工具 ────────────────────────────────────────────────────────────────────────
class TeamTaskCreateTool(_TeamTool):
    name = constants.TOOL_TASK_CREATE
    description = (
        "在团队共享任务清单里新建一条任务（可带依赖 deps 与负责文件集 files）。依赖全部 done 后任务"
        "自动解锁可领。Lead 拆解目标时用它把子任务写进清单。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务标题 / 描述"},
            "id": {"type": "string", "description": "可选任务 id（缺省自动分配）"},
            "deps": {"type": "array", "items": {"type": "string"}, "description": "依赖的任务 id 列表"},
            "assignee": {"type": "string", "description": "可选：指派给某队员（缺省谁都可领）"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "负责的文件集"},
        },
        "required": ["title"],
    }

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        err = self._need_team()
        if err is not None:
            return err
        title = str(arguments.get("title", "")).strip()
        if not title:
            return ToolResult.fail(f"{self.name} 失败：缺少 title")
        task_id = str(arguments.get("id", "")).strip() or self._auto_id()
        deps = [str(x) for x in (arguments.get("deps") or [])]
        files = [str(x) for x in (arguments.get("files") or [])]
        assignee = arguments.get("assignee") or None
        task = self._m.task_store.create(
            task_id, title=title, deps=deps, assignee=assignee, files=files
        )
        return ToolResult.ok(
            f"已建任务「{task.id}」（状态={task.status}，依赖={deps or '无'}，文件集={files or '无'}）"
        )

    def _auto_id(self) -> str:
        existing = {t.id for t in self._m.task_store.list()}
        i = 1
        while f"task-{i}" in existing:
            i += 1
        return f"task-{i}"


class TeamTaskUpdateTool(_TeamTool):
    name = constants.TOOL_TASK_UPDATE
    description = (
        "更新一条共享任务的字段（如把 status 置为 done、改 assignee / files）。队员做完任务后用它"
        "把状态置 done（依赖它的任务随之解锁）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "要更新的任务 id"},
            "status": {
                "type": "string",
                "enum": list(constants.TASK_STATUSES),
                "description": "新状态（blocked/pending/in_progress/done）",
            },
            "title": {"type": "string"},
            "assignee": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id"],
    }

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        err = self._need_team()
        if err is not None:
            return err
        task_id = str(arguments.get("id", "")).strip()
        if not task_id:
            return ToolResult.fail(f"{self.name} 失败：缺少 id")
        changes: dict = {}
        if "status" in arguments and arguments["status"]:
            status = str(arguments["status"])
            if status not in constants.TASK_STATUSES:
                return ToolResult.fail(f"{self.name} 失败：非法状态「{status}」")
            changes["status"] = status
        if "title" in arguments and arguments["title"] is not None:
            changes["title"] = str(arguments["title"])
        if "assignee" in arguments:
            changes["assignee"] = arguments["assignee"] or None
        if "files" in arguments and arguments["files"] is not None:
            changes["files"] = [str(x) for x in arguments["files"]]
        task = self._m.task_store.update(task_id, **changes)
        if task is None:
            return ToolResult.fail(f"{self.name} 失败：未找到任务「{task_id}」")
        return ToolResult.ok(f"已更新任务「{task.id}」（状态={task.status}）")


class TeamTaskListTool(_TeamTool):
    name = constants.TOOL_TASK_LIST
    description = "列出团队共享任务清单（id / 标题 / 状态 / 依赖 / 认领者）；用于了解当前进度与可领任务。"
    parameters = {"type": "object", "properties": {}}

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        err = self._need_team()
        if err is not None:
            return err
        tasks = self._m.task_store.list()
        if not tasks:
            return ToolResult.ok("（任务清单为空）")
        lines = [
            f"- {t.id} [{t.status}] {t.title}"
            f"{'，依赖=' + ','.join(t.deps) if t.deps else ''}"
            f"{'，认领=' + t.claimed_by if t.claimed_by else ''}"
            for t in tasks
        ]
        return ToolResult.ok("团队任务清单：\n" + "\n".join(lines))


class TeamTaskClaimTool(_TeamTool):
    name = constants.TOOL_TASK_CLAIM
    description = (
        "从共享清单认领一条「未阻塞且（指派给我或未指派）」的任务（文件锁原子化，防多人同领）。"
        "队员领活时调用，参数 member 填自己的名字。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "member": {"type": "string", "description": "认领者队员名（填自己的名字）"},
        },
    }

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        err = self._need_team()
        if err is not None:
            return err
        member = str(arguments.get("member", "")).strip() or self._m.lead_name
        task = self._m.task_store.claim(member)
        if task is None:
            return ToolResult.ok(f"当前没有「{member}」可领的任务")
        return ToolResult.ok(
            f"已认领任务「{task.id}」：{task.title}（文件集={task.files or '无'}）"
        )


class SendMessageTool(_TeamTool):
    name = constants.TOOL_SEND_MESSAGE
    description = (
        "给指定队员或 Lead 发一条消息（点对点）。要广播就对每个收件人各发一条。消息自动投递、收件人"
        "下一轮自动收到。参数 from 填自己的名字、to 填收件人名字。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "收件人名字（队员名或 Lead）"},
            "body": {"type": "string", "description": "消息正文"},
            "from": {"type": "string", "description": "发件人名字（填自己；缺省按 Lead）"},
            "kind": {
                "type": "string",
                "enum": list(constants.PROTOCOL_KINDS),
                "description": "可选：结构化协议消息类型（缺省为普通消息）",
            },
        },
        "required": ["to", "body"],
    }

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        if self._m.team is None or self._m.messenger is None:
            return ToolResult.fail("当前没有团队（请先 create_team）")
        to = str(arguments.get("to", "")).strip()
        body = str(arguments.get("body", ""))
        if not to or not body:
            return ToolResult.fail(f"{self.name} 失败：缺少 to / body")
        sender = str(arguments.get("from", "")).strip() or self._m.lead_name
        kind = arguments.get("kind") or None
        if kind is not None and kind not in constants.PROTOCOL_KINDS:
            return ToolResult.fail(f"{self.name} 失败：非法 kind「{kind}」")
        # 恢复语义（T13）：收件人是花名册里的队员但已不在跑（会话恢复后 in-process 队员不复活）→
        # 提示重新 spawn，不假装「从磁盘恢复上下文继续指派」。Lead 自身始终是合法收件人。
        if (
            to != self._m.lead_name
            and to in self._m.team.member_names()
            and not self._m.is_member_live(to)
        ):
            return ToolResult.ok(constants.RESPAWN_NOTICE.format(name=to))
        from coreagent.teams.messaging import UnknownRecipient

        try:
            self._m.messenger.send(sender, to, body, kind=kind)
        except UnknownRecipient as e:
            return ToolResult.fail(f"{self.name} 失败：{e}")
        return ToolResult.ok(f"已把消息投递给「{to}」")


# ── 管理工具（仅 Lead）──────────────────────────────────────────────────────────────
class CreateTeamTool(_TeamTool):
    name = constants.TOOL_CREATE_TEAM
    description = (
        "创建一个长期存在的团队（一个 Lead 同时只一个团队；要换队先 disband_team）。建好后即可"
        "用 team_task_create 拆任务、spawn_members 派生队员。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "团队名"},
            "backend": {
                "type": "string",
                "enum": list(constants.BACKENDS),
                "description": "运行后端（缺省 auto；本期分屏回落 in-process）",
            },
        },
        "required": ["name"],
    }

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return ToolResult.fail(f"{self.name} 失败：缺少团队名 name")
        backend = str(arguments.get("backend", constants.BACKEND_AUTO))
        from coreagent.teams.store import TeamExistsError

        try:
            team = self._m.create_team(name, backend=backend)
        except TeamExistsError as e:
            return ToolResult.fail(f"{self.name} 失败：{e}")
        return ToolResult.ok(f"已创建团队「{team.name}」（Lead={team.lead}，后端={backend}）")


class SpawnMembersTool(_TeamTool):
    name = constants.TOOL_SPAWN_MEMBERS
    description = (
        "派生一个或多个长驻队员（in-process 真并行）；可指定角色（复用已有子 Agent 角色）与是否需"
        "审批。队员统一继承 Lead 当前权限模式。派生后队员从共享清单领活、与同伴协作。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "队员名（点对点消息收件人）"},
                        "role": {"type": "string", "description": "可选：复用的子 Agent 角色名"},
                        "needs_approval": {"type": "boolean", "description": "是否需 Lead 审批"},
                    },
                    "required": ["name"],
                },
                "description": "要派生的队员列表",
            },
        },
        "required": ["members"],
    }

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        err = self._need_team()
        if err is not None:
            return err
        specs = arguments.get("members") or []
        if not specs:
            return ToolResult.fail(f"{self.name} 失败：members 为空")
        spawned: list[str] = []
        failed: list[str] = []
        for spec in specs:
            name = str(spec.get("name", "")).strip()
            if not name:
                failed.append("(缺名字)")
                continue
            try:
                self._m.spawn_member(
                    name,
                    role=spec.get("role") or None,
                    needs_approval=bool(spec.get("needs_approval", False)),
                )
                spawned.append(name)
            except Exception as e:  # noqa: BLE001 —— 单个队员派生失败不影响其余
                logger.warning("派生队员 %s 失败：%r", name, e)
                failed.append(f"{name}（{type(e).__name__}）")
        msg = f"已派生 {len(spawned)} 个队员：{'、'.join(spawned) or '无'}"
        if failed:
            msg += f"；失败：{'、'.join(failed)}"
        return ToolResult.ok(msg)


class DisbandTeamTool(_TeamTool):
    name = constants.TOOL_DISBAND_TEAM
    description = "解散当前团队（取消在跑的队员、清团队配置）。要换队或收工时调用。"
    parameters = {"type": "object", "properties": {}}

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        if self._m.team is None:
            return ToolResult.ok("当前没有团队，无需解散")
        name = self._m.team.name
        self._m.disband()
        return ToolResult.ok(f"已解散团队「{name}」")


# ── 工厂：建全部团队工具实例（供 main 注册进 registry）─────────────────────────────────
def build_team_tools(manager) -> list[Tool]:
    """构造全部团队工具实例（协作 + 管理），持同一 TeamManager。"""
    return [
        TeamTaskCreateTool(manager),
        TeamTaskUpdateTool(manager),
        TeamTaskListTool(manager),
        TeamTaskClaimTool(manager),
        SendMessageTool(manager),
        CreateTeamTool(manager),
        SpawnMembersTool(manager),
        DisbandTeamTool(manager),
    ]
