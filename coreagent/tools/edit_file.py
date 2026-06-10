"""edit_file：原文唯一匹配替换（需确认）。

old_string 必须在目标文件中恰好出现一次：0 处或多处都返回清楚的错误文案，
让模型据此提供更长、唯一的上下文后重试。
"""

import os
import tempfile
from pathlib import Path

from coreagent.tools.base import PathEscapeError, Tool, ToolResult, resolve_confined


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "在文件中把唯一匹配的 old_string 替换为 new_string。"
        "编辑前先读：先用 read_file 确认目标文件真实内容，再据原文唯一匹配替换。"
        "old_string 须在文件中恰好出现一次；若匹配 0 处或多处会失败，请提供更长、唯一的上下文。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要修改的文件路径"},
            "old_string": {"type": "string", "description": "待替换的原文片段（须唯一）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    requires_confirmation = True

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        path = arguments.get("path", "")
        old_string = arguments.get("old_string", "")
        new_string = arguments.get("new_string", "")
        try:
            p = resolve_confined(cwd, path)
        except PathEscapeError as e:
            return ToolResult.fail(f"edit_file 失败：{e}")
        if not p.exists() or not p.is_file():
            return ToolResult.fail(f"edit_file 失败：文件不存在：{path}")
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult.fail(f"edit_file 失败：读取出错：{e}")

        count = text.count(old_string)
        if count == 0:
            return ToolResult.fail(
                "edit_file 失败：未找到要替换的文本（old_string 在文件中 0 处匹配）"
            )
        if count > 1:
            return ToolResult.fail(
                f"edit_file 失败：old_string 在文件中匹配到 {count} 处，请提供更长、唯一的上下文"
            )

        new_text = text.replace(old_string, new_string, 1)
        try:
            self._atomic_write(p, new_text)
        except OSError as e:
            return ToolResult.fail(f"edit_file 失败：写入出错：{e}")
        return ToolResult.ok(f"已替换 {path} 中 1 处匹配")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
