"""T3｜团队配置持久化 + 单团队约束。

落盘 ``~/.codeagent/teams/{team-name}/config.json``：原子写（临时文件 + ``os.replace``，经
``locking.write_json_atomic``）、读写并发用 T2 文件锁。members 数组每项记 名字 / agent id /
agent type（队员可读它发现同伴）+ 运行态（session-id / pane-id，系统维护、用户不手改）。

单团队约束（spec 能力 4 / Out of Scope）：一个 Lead（= 一个 TeamStore 实例）同时只一个团队；
建新团队前须先 ``disband`` 当前团队。**无嵌套 / 无领导权转移**在此层固定：建团队的会话身份即
Lead，store 不提供「把队员提升为 Lead」或「转移」的入口。
"""

import logging
from pathlib import Path

from coreagent.teams import constants
from coreagent.teams.locking import file_lock, read_json, write_json_atomic
from coreagent.teams.types import Member, Team

logger = logging.getLogger(__name__)


class TeamExistsError(Exception):
    """已有当前团队时再建新团队（单团队约束）；文案含 SINGLE_TEAM_ERROR。"""


def _member_to_dict(m: Member) -> dict:
    """成员 → 落盘字典（kebab-case 键，精确字段名见 constants）。"""
    return {
        constants.MEMBER_FIELD_NAME: m.name,
        constants.MEMBER_FIELD_AGENT_ID: m.agent_id,
        constants.MEMBER_FIELD_AGENT_TYPE: m.agent_type,
        constants.MEMBER_FIELD_SESSION_ID: m.session_id,
        constants.MEMBER_FIELD_PANE_ID: m.pane_id,
        "role": m.role,
        "cwd": m.cwd,
        "needs-approval": m.needs_approval,
    }


def _member_from_dict(d: dict) -> Member:
    return Member(
        name=d.get(constants.MEMBER_FIELD_NAME, ""),
        role=d.get("role"),
        agent_id=d.get(constants.MEMBER_FIELD_AGENT_ID, ""),
        agent_type=d.get(constants.MEMBER_FIELD_AGENT_TYPE, constants.BACKEND_IN_PROCESS),
        cwd=d.get("cwd", ""),
        needs_approval=bool(d.get("needs-approval", False)),
        session_id=d.get(constants.MEMBER_FIELD_SESSION_ID, ""),
        pane_id=d.get(constants.MEMBER_FIELD_PANE_ID, ""),
    )


def _team_to_dict(t: Team) -> dict:
    return {
        "name": t.name,
        "lead": t.lead,
        "backend": t.backend,
        "members": [_member_to_dict(m) for m in t.members],
    }


def _team_from_dict(d: dict) -> Team:
    return Team(
        name=d.get("name", ""),
        lead=d.get("lead", ""),
        backend=d.get("backend", constants.BACKEND_AUTO),
        members=[_member_from_dict(m) for m in d.get("members", [])],
    )


class TeamStore:
    """一个 Lead 会话的团队配置存储（持当前团队引用，执行单团队约束）。"""

    def __init__(self, root: Path | str | None = None) -> None:
        # root 覆盖（测试 / 自定义）：缺省取 constants.TEAMS_DIR（~/.codeagent/teams）。
        self.root = Path(root) if root is not None else constants.TEAMS_DIR
        self._current: Team | None = None

    # ── 路径 ────────────────────────────────────────────────────────────────────
    def team_dir(self, name: str) -> Path:
        return self.root / name

    def config_path(self, name: str) -> Path:
        return self.team_dir(name) / constants.TEAM_CONFIG_FILENAME

    # ── 当前团队（单团队约束）─────────────────────────────────────────────────────
    @property
    def current(self) -> Team | None:
        return self._current

    def create_team(self, name: str, *, lead: str = "", backend: str = constants.BACKEND_AUTO) -> Team:
        """建一个新团队并落盘；已有当前团队则抛 TeamExistsError（须先 disband）。"""
        if self._current is not None:
            raise TeamExistsError(
                constants.SINGLE_TEAM_ERROR.format(name=self._current.name)
            )
        team = Team(name=name, lead=lead, backend=backend)
        self.save(team)
        self._current = team
        return team

    def disband(self) -> None:
        """清理当前团队（删配置目录、释放约束）；无当前团队则 no-op。"""
        if self._current is None:
            return
        self._remove_dir(self.team_dir(self._current.name))
        self._current = None

    # ── 持久化 ──────────────────────────────────────────────────────────────────
    def save(self, team: Team) -> None:
        """原子写团队配置（持锁防并发半写）。"""
        path = self.config_path(team.name)
        with file_lock(path):
            write_json_atomic(path, _team_to_dict(team))

    def load(self, name: str) -> Team | None:
        """从盘读团队配置；缺失 / 损坏 → None。"""
        data = read_json(self.config_path(name), default=None)
        if not isinstance(data, dict):
            return None
        return _team_from_dict(data)

    def add_member(self, member: Member) -> None:
        """把一个队员加入当前团队并落盘（持锁，重名则替换同名项）。"""
        if self._current is None:
            raise ValueError("无当前团队，无法加入成员（请先 create_team）")
        path = self.config_path(self._current.name)
        with file_lock(path):
            data = read_json(path, default=None)
            team = _team_from_dict(data) if isinstance(data, dict) else self._current
            team.members = [m for m in team.members if m.name != member.name]
            team.members.append(member)
            write_json_atomic(path, _team_to_dict(team))
            self._current = team

    @staticmethod
    def _remove_dir(d: Path) -> None:
        import shutil
        try:
            shutil.rmtree(d)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("清理团队目录失败：%r", e)
