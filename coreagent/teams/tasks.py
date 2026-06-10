"""T4｜共享任务存储（CRUD + 依赖解锁 + Hybrid 认领）。

落盘 ``~/.codeagent/tasks/{team-name}/``，每任务一文件 ``{id}.json``。多队员并发读写经 T2 文件锁
串行化（认领防双取）。

依赖解锁：任务带 ``deps``（依赖任务 id 列表）；依赖**全部 done** 时，该任务由 ``blocked`` 自动转
``pending``（可领）。无依赖的任务建出来即 ``pending``。

Hybrid 认领：队员只取「``status==pending`` 且（``assignee==我`` 或 ``assignee`` 为空）」的任务；
认领走文件锁原子地置 ``in_progress`` + ``claimed-by``，故并发下同一任务恰一个赢家、其余让出。
"""

from __future__ import annotations  # 类内有 list() 方法，避免 list[Task] 注解被解析成方法下标

import logging
import time
from pathlib import Path

from coreagent.teams import constants
from coreagent.teams.locking import file_lock, read_json, write_json_atomic
from coreagent.teams.types import Task

logger = logging.getLogger(__name__)


def _task_to_dict(t: Task) -> dict:
    return {
        constants.TASK_FIELD_ID: t.id,
        constants.TASK_FIELD_TITLE: t.title,
        constants.TASK_FIELD_DEPS: list(t.deps),
        constants.TASK_FIELD_ASSIGNEE: t.assignee,
        constants.TASK_FIELD_FILES: list(t.files),
        constants.TASK_FIELD_STATUS: t.status,
        constants.TASK_FIELD_CLAIMED_BY: t.claimed_by,
        constants.TASK_FIELD_CREATED: t.created,
        constants.TASK_FIELD_UPDATED: t.updated,
    }


def _task_from_dict(d: dict) -> Task:
    return Task(
        id=d.get(constants.TASK_FIELD_ID, ""),
        title=d.get(constants.TASK_FIELD_TITLE, ""),
        deps=list(d.get(constants.TASK_FIELD_DEPS, []) or []),
        assignee=d.get(constants.TASK_FIELD_ASSIGNEE),
        files=list(d.get(constants.TASK_FIELD_FILES, []) or []),
        status=d.get(constants.TASK_FIELD_STATUS, constants.TASK_BLOCKED),
        claimed_by=d.get(constants.TASK_FIELD_CLAIMED_BY),
        created=d.get(constants.TASK_FIELD_CREATED, 0.0),
        updated=d.get(constants.TASK_FIELD_UPDATED, 0.0),
    )


class TaskStore:
    """一个团队的共享任务存储（落盘 + 锁 + 依赖解锁 + Hybrid 认领）。"""

    def __init__(self, team_name: str, root: Path | str | None = None, *, clock=time.time) -> None:
        base = Path(root) if root is not None else constants.TASKS_DIR
        self.dir = base / team_name
        self._clock = clock     # 注入时钟便于测试（时间戳在写入侧生成）

    # ── 路径 ────────────────────────────────────────────────────────────────────
    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    # ── CRUD ────────────────────────────────────────────────────────────────────
    def create(
        self,
        task_id: str,
        *,
        title: str = "",
        deps: list[str] | None = None,
        assignee: str | None = None,
        files: list[str] | None = None,
    ) -> Task:
        """新建任务并落盘。无依赖 → 初始 ``pending``；有依赖 → ``blocked``。"""
        deps = list(deps or [])
        now = self._clock()
        status = constants.TASK_PENDING if not deps else constants.TASK_BLOCKED
        task = Task(
            id=task_id, title=title, deps=deps, assignee=assignee,
            files=list(files or []), status=status, created=now, updated=now,
        )
        with file_lock(self._path(task_id)):
            write_json_atomic(self._path(task_id), _task_to_dict(task))
        # 新建后重算解锁（若其依赖此刻已全 done，本任务也应可领）。
        self._recompute_unlocks()
        return self.get(task_id)

    def get(self, task_id: str) -> Task | None:
        d = read_json(self._path(task_id), default=None)
        return _task_from_dict(d) if isinstance(d, dict) else None

    def list(self) -> list[Task]:
        """列出全部任务（按 id 排序，稳定）。"""
        if not self.dir.exists():
            return []
        out: list[Task] = []
        for p in sorted(self.dir.glob("*.json")):
            d = read_json(p, default=None)
            if isinstance(d, dict):
                out.append(_task_from_dict(d))
        return out

    def update(self, task_id: str, **changes) -> Task | None:
        """改任务字段（持锁读改写）；状态变更后重算依赖解锁。返回改后任务。

        ``changes`` 键用模型属性名（title/deps/assignee/files/status/claimed_by）。
        """
        path = self._path(task_id)
        with file_lock(path):
            d = read_json(path, default=None)
            if not isinstance(d, dict):
                return None
            task = _task_from_dict(d)
            for k, v in changes.items():
                if hasattr(task, k):
                    setattr(task, k, v)
            task.updated = self._clock()
            write_json_atomic(path, _task_to_dict(task))
        # 状态可能转 done → 解锁依赖它的任务。
        self._recompute_unlocks()
        return self.get(task_id)

    def delete(self, task_id: str) -> bool:
        path = self._path(task_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    # ── 依赖解锁 ────────────────────────────────────────────────────────────────
    def _recompute_unlocks(self) -> None:
        """扫全部任务：``blocked`` 且 ``deps`` 全部 done → 转 ``pending``（可领）。

        逐个持锁改写（幂等：已是 pending / 别状态不动）。不把 pending/in_progress/done 改回去。
        """
        tasks = self.list()
        done_ids = {t.id for t in tasks if t.status == constants.TASK_DONE}
        for t in tasks:
            if t.status != constants.TASK_BLOCKED:
                continue
            if all(dep in done_ids for dep in t.deps):
                path = self._path(t.id)
                with file_lock(path):
                    d = read_json(path, default=None)
                    if not isinstance(d, dict):
                        continue
                    cur = _task_from_dict(d)
                    if cur.status == constants.TASK_BLOCKED and all(
                        dep in done_ids for dep in cur.deps
                    ):
                        cur.status = constants.TASK_PENDING
                        cur.updated = self._clock()
                        write_json_atomic(path, _task_to_dict(cur))

    # ── Hybrid 认领 ─────────────────────────────────────────────────────────────
    def claimable(self, member: str) -> list[Task]:
        """该队员**可领**的任务：``status==pending`` 且（``assignee==member`` 或 assignee 为空）。

        指派给我的优先（排前），其次未指派的；只读、不改状态。
        """
        out = [
            t for t in self.list()
            if t.status == constants.TASK_PENDING
            and (not t.assignee or t.assignee == member)
        ]
        out.sort(key=lambda t: (0 if t.assignee == member else 1, t.id))
        return out

    def claim(self, member: str) -> Task | None:
        """为队员认领一个任务（Hybrid + 文件锁防双取）；无可领则 None。

        逐个候选持锁、重读校验仍可领 → 原子置 ``in_progress`` + ``claimed-by`` 返回；被别人抢走
        （重读已非 pending）→ 让出、试下一个。故并发下同一任务恰一个赢家。
        """
        for cand in self.claimable(member):
            path = self._path(cand.id)
            with file_lock(path):
                d = read_json(path, default=None)
                if not isinstance(d, dict):
                    continue
                task = _task_from_dict(d)
                # 重读校验：仍 pending 且仍可领（防双取的关键——锁内复核真值）。
                if task.status != constants.TASK_PENDING:
                    continue
                if task.assignee and task.assignee != member:
                    continue
                task.status = constants.TASK_IN_PROGRESS
                task.claimed_by = member
                task.updated = self._clock()
                write_json_atomic(path, _task_to_dict(task))
                return task
        return None
