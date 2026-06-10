"""T7/T8｜会话摘要存档与恢复。

T7 存档：每轮自然停下后，用 #0009 摘要器（coreagent/context/summarize.Summarizer，5 段纪律）
        把当前会话压成摘要写入 per-session summary.md（摘要而非完整消息流）。
T8 恢复：启动时找本项目最近一次会话的 summary.md 注入——
        缺失 → 空上下文；token 超限 → 先压缩（复用同一摘要器）再注入、并兜底截断保证不超预算；
        距上次会话过久（> resume_gap_hours）→ 注入时间跨度提醒。
"""

import logging
from datetime import datetime
from pathlib import Path

from coreagent.context.constants import CHARS_PER_TOKEN
from coreagent.context.summarize import Summarizer  # 复用 #0009 摘要器（5 段纪律）
from coreagent.memory import constants, paths

logger = logging.getLogger(__name__)


def _write_text_atomic(path: Path, text: str) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _est_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


class SessionMemory:
    """单进程的会话记忆：持当前 session-id，负责摘要存档与启动恢复。"""

    def __init__(
        self,
        project_dir: Path | str,
        config,
        provider,
        *,
        root: Path | str | None = None,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.config = config
        self.root = Path(root) if root else (
            Path(config.memory.root) if getattr(config, "memory", None) and config.memory.root
            else constants.DEFAULT_ROOT
        )
        self.session_id = session_id or paths.new_session_id(now)
        # 复用 #0009 摘要器（同一压缩纪律，不另起一套）。
        self.summarizer = Summarizer(provider)

    # ── T7 存档 ───────────────────────────────────────────────────────────────────

    async def update_summary(self, messages: list[dict]) -> bool:
        """用摘要器把 messages 压成摘要写入本会话 summary.md。成功 True；空/失败/熔断 False。"""
        if not messages:
            return False
        try:
            summary = await self.summarizer.summarize(messages)
        except Exception as e:  # noqa: BLE001 —— 摘要存档失败不影响主流程
            logger.warning("会话摘要生成失败，跳过本轮存档：%r", e)
            return False
        if not summary:
            return False
        path = paths.summary_path(self.project_dir, self.session_id, self.root)
        try:
            _write_text_atomic(path, summary)
        except OSError as e:
            logger.warning("会话摘要落盘失败：%r", e)
            return False
        return True

    # ── T8 恢复 ───────────────────────────────────────────────────────────────────

    async def recover(self, now: datetime | None = None) -> str:
        """加载本项目最近一次会话的摘要，返回注入文本（可空）。

        缺失 → 空串；距上次过久 → 前置时间跨度提醒；token 超限 → 先压缩再截断兜底（不超预算）。
        """
        prev = paths.latest_session(self.project_dir, exclude=self.session_id, root=self.root)
        if prev is None:
            return ""
        path = paths.summary_path(self.project_dir, prev, self.root)
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning("读取上次会话摘要失败，以空上下文启动：%r", e)
            return ""
        if not text:
            return ""

        # token 超限 → 先复用摘要器压缩，再硬截断兜底，保证注入不超预算。
        if _est_tokens(text) > constants.RESUME_TOKEN_BUDGET:
            text = await self._shrink(text)

        # 时间跨度提醒：距上次会话超过阈值则前置一行提醒。
        parts: list[str] = []
        reminder = self._gap_reminder(prev, now)
        if reminder:
            parts.append(reminder)
        parts.append(text)
        return "\n\n".join(parts)

    async def _shrink(self, text: str) -> str:
        """把超预算的摘要再压缩一轮（复用摘要器）；仍超则硬截断到预算字符上限。"""
        try:
            shorter = await self.summarizer.summarize([{"role": "user", "content": text}])
            if shorter:
                text = shorter
        except Exception as e:  # noqa: BLE001 —— 压缩失败则仅靠下面的截断兜底
            logger.warning("恢复摘要压缩失败，改用截断兜底：%r", e)
        budget_chars = constants.RESUME_TOKEN_BUDGET * CHARS_PER_TOKEN
        if len(text) > budget_chars:
            text = text[:budget_chars].rstrip() + "…（摘要已截断）"
        return text

    def _gap_reminder(self, prev_session: str, now: datetime | None) -> str | None:
        ts = paths.session_id_time(prev_session)
        if ts is None:
            return None
        gap_hours = ((now or datetime.now()) - ts).total_seconds() / 3600
        if gap_hours > self.config.memory.resume_gap_hours:
            return constants.RESUME_GAP_REMINDER.format(hours=int(gap_hours))
        return None
