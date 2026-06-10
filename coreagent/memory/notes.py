"""T5/T6｜自动笔记：四类（用户偏好/纠正反馈/项目知识/参考资料）存储 + 索引 + 每轮 LLM 更新。

存储（两级分目录）：
  用户级（全局）  <root>/memory/notes/                 —— 承载 type ∈ {user, feedback}
  项目级（按哈希）<root>/projects/<hash>/memory/notes/  —— 承载 type ∈ {project, reference}
每条笔记 = 一份带 frontmatter（name/description/type）的独立 Markdown；每级一个 INDEX.md，
索引受行数 / 体积上限约束（超限丢弃最旧条目）。

每轮自然停下后调主模型（= config.model）：基于「现有索引 + 本轮内容」判断是否值得记 + 去重，
输出 JSON 笔记数组（允许空数组 = 空操作）。解析失败 / 调用失败 → 空操作（失败软化）。
"""

import json
import logging
import re
from pathlib import Path

from coreagent.memory import constants, paths
from coreagent.providers.base import ChunkType

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _slugify(name: str) -> str:
    """把任意 name 规整为安全文件名 slug（小写、仅 [a-z0-9_-]）。"""
    s = _SLUG_RE.sub("-", str(name).strip().lower()).strip("-")
    return s or "note"


def _frontmatter(note: dict) -> str:
    """渲染一条笔记的完整 Markdown（frontmatter: name/description/type + 正文）。"""
    name = note.get("name", "note")
    desc = str(note.get("description", "")).replace("\n", " ")
    ntype = note.get("type", "project")
    body = str(note.get("body", "")).strip()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        f"type: {ntype}\n"
        "---\n\n"
        f"{body}\n"
    )


def _write_text_atomic(path: Path, text: str) -> None:
    """原子写文本（同目录 temp → replace）；复用与 JSON 同款落盘范式。"""
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


# ── 笔记更新 LLM 提示 ──────────────────────────────────────────────────────────────

NOTES_SYSTEM = (
    "你是编程助手的长期记忆维护器。你的唯一任务是判断本轮对话里有没有「未来仍有用」的信息"
    "值得记成长期笔记，并与现有笔记去重。只输出 JSON，不要调用任何工具、不要输出其它文字。"
)

# JSON 协议说明（拼在「现有索引 + 本轮内容」之前）。
NOTES_INSTRUCTION = (
    "请基于【现有笔记索引】与【本轮对话】，判断有没有值得长期记住的信息。\n\n"
    "判定纪律：\n"
    "1. 只记「跨会话仍有用」的：用户稳定偏好、对你工作方式的纠正、项目的非显然约定/事实、"
    "外部参考资料。一次性的闲聊 / 临时上下文 / 能从代码或 git 直接看出的事实，不要记。\n"
    "2. 去重：若信息已在现有索引里，不要重复记（除非是实质性更新——此时用相同 name 覆盖）。\n"
    "3. 没有值得记的就输出空数组 []。\n\n"
    "输出格式：一个 JSON 数组，每个元素形如 "
    '{"type": "user|feedback|project|reference", "name": "kebab-case-短标识", '
    '"description": "一行摘要", "body": "要记住的事实正文"}。type 含义：'
    "user=用户偏好、feedback=纠正反馈、project=项目知识、reference=参考资料。"
    "只输出该 JSON 数组本身。\n\n"
)


def _parse_notes(raw: str) -> list[dict]:
    """从模型输出里宽松提取 JSON 笔记数组；失败 / 非数组 → 空列表（空操作）。"""
    raw = raw.strip()
    # 去掉常见 ```json 围栏。
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("\n") + 1:] if "\n" in raw else raw
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(raw[start: end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ntype = item.get("type")
        if ntype not in constants.NOTE_TYPES:
            continue
        body = str(item.get("body", "")).strip()
        if not body:
            continue
        out.append(item)
    return out


class NotesStore:
    """四类自动笔记的存储 / 索引 / 每轮 LLM 更新。"""

    def __init__(
        self,
        project_dir: Path | str,
        config,
        root: Path | str | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.config = config
        self.root = Path(root) if root else (
            Path(config.memory.root) if getattr(config, "memory", None) and config.memory.root
            else constants.DEFAULT_ROOT
        )

    # ── 目录定位 ──────────────────────────────────────────────────────────────────

    def _level_dir(self, level: str) -> Path:
        if level == constants.NOTE_LEVEL_USER:
            return self.root / constants.NOTES_RELDIR
        return paths.project_dir_path(self.project_dir, self.root) / constants.NOTES_RELDIR

    def _level_for_type(self, ntype: str) -> str:
        return constants.NOTE_TYPE_LEVEL.get(ntype, constants.NOTE_LEVEL_PROJECT)

    def index_path(self, level: str) -> Path:
        return self._level_dir(level) / constants.NOTES_INDEX_FILENAME

    # ── 写一条笔记 + 重建索引 ──────────────────────────────────────────────────────

    def write_note(self, note: dict) -> Path:
        """把一条笔记写入对应级别目录（按 name 去重覆盖），并重建该级索引。返回笔记文件路径。"""
        ntype = note["type"]
        level = self._level_for_type(ntype)
        level_dir = self._level_dir(level)
        slug = _slugify(note.get("name") or note.get("description") or ntype)
        note = {**note, "name": slug}
        note_path = level_dir / f"{slug}.md"
        _write_text_atomic(note_path, _frontmatter(note))
        self._rebuild_index(level)
        return note_path

    def _iter_notes(self, level: str) -> list[tuple[float, str, dict]]:
        """读取某级所有笔记文件，解析 frontmatter，返回 (mtime, slug, fields) 列表。"""
        level_dir = self._level_dir(level)
        items: list[tuple[float, str, dict]] = []
        if not level_dir.exists():
            return items
        for p in level_dir.glob("*.md"):
            if p.name == constants.NOTES_INDEX_FILENAME:
                continue
            try:
                fields = _read_frontmatter(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            items.append((p.stat().st_mtime, p.stem, fields))
        return items

    def _rebuild_index(self, level: str) -> None:
        """重建某级 INDEX.md：每条笔记一行；超行数 / 体积上限则丢最旧（保留最新）。"""
        items = self._iter_notes(level)
        items.sort(key=lambda t: t[0], reverse=True)  # 新→旧
        mem = self.config.memory
        header = "# 记忆索引\n\n"
        lines: list[str] = []
        for _mtime, slug, fields in items:
            label = constants.NOTE_TYPE_LABELS.get(fields.get("type", ""), "")
            desc = fields.get("description", "") or slug
            line = f"- [{desc}]({slug}.md) — {label}"
            candidate = lines + [line]
            # 行数上限（含 header 2 行）+ 体积上限：任一超限即停止追加（已按新→旧，保最新）。
            text = header + "\n".join(candidate) + "\n"
            if len(candidate) > max(1, mem.index_max_lines - 2):
                break
            if len(text.encode("utf-8")) > mem.index_max_bytes:
                break
            lines = candidate
        _write_text_atomic(self.index_path(level), header + "\n".join(lines) + "\n")

    # ── 注入用：渲染两级索引 ───────────────────────────────────────────────────────

    def render_index(self) -> str:
        """合并用户级 + 项目级索引内容（供启动注入）；都为空则空串。"""
        sections: list[str] = []
        for level, name in ((constants.NOTE_LEVEL_USER, "用户级"), (constants.NOTE_LEVEL_PROJECT, "项目级")):
            path = self.index_path(level)
            if path.exists():
                try:
                    body = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                # 仅当有真实条目（含 "- " 行）才纳入，空索引不注入噪声。
                if "\n- " in ("\n" + body):
                    sections.append(f"[{name}]\n{body}")
        return "\n\n".join(sections)

    # ── 每轮 LLM 更新 ──────────────────────────────────────────────────────────────

    async def update(self, provider, conversation_text: str) -> list[Path]:
        """调主模型判断本轮是否值得记 + 去重，写入对应类别。返回新写入的笔记路径列表（可空）。

        失败软化：provider 调用 / 解析任一失败 → 返回空列表，不抛、不影响主流程。
        """
        if not conversation_text.strip():
            return []
        existing = self.render_index() or "（暂无）"
        user_msg = {
            "role": "user",
            "content": (
                NOTES_INSTRUCTION
                + "【现有笔记索引】\n" + existing
                + "\n\n【本轮对话】\n" + conversation_text
            ),
        }
        try:
            parts: list[str] = []
            async for chunk in provider.stream_chat([user_msg], system=NOTES_SYSTEM, tools=None):
                if chunk.type == ChunkType.TEXT:
                    parts.append(chunk.content)
        except Exception as e:  # noqa: BLE001 —— 笔记更新失败不影响主流程
            logger.warning("自动笔记 LLM 更新失败，跳过本轮：%r", e)
            return []
        notes = _parse_notes("".join(parts))
        written: list[Path] = []
        for note in notes:
            try:
                written.append(self.write_note(note))
            except OSError as e:
                logger.warning("写入自动笔记失败，跳过：%r", e)
        if written:
            logger.info("自动笔记新增/更新 %d 条", len(written))
        return written


def _read_frontmatter(text: str) -> dict:
    """从笔记 Markdown 头部解析 frontmatter 的 name/description/type；无则空 dict。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields: dict = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields
