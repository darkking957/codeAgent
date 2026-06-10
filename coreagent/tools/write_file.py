"""write_file：覆盖写文件并自动建父目录（需确认）。

原子写沿用 `conversation.py: Conversation.save` 思路：同目录临时文件 + fsync + os.replace，
写入被中断也不会留下半截文件。
"""

import os
import tempfile
from pathlib import Path

from coreagent.tools.base import PathEscapeError, Tool, ToolResult, resolve_confined


class WriteFileTool(Tool):
    name = "write_file"
    description = "把给定内容覆盖写入指定路径（自动创建父目录）。需要新建或整体改写文件时调用。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径"},
            "content": {"type": "string", "description": "要写入的完整文本内容"},
        },
        "required": ["path", "content"],
    }
    requires_confirmation = True

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        path = arguments.get("path", "")
        content = arguments.get("content", "")
        try:
            p = resolve_confined(cwd, path)
        except PathEscapeError as e:
            return ToolResult.fail(f"write_file 失败：{e}")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(p, content)
        except OSError as e:
            return ToolResult.fail(f"write_file 失败：写入出错：{e}")
        return ToolResult.ok(f"已写入 {path}（{len(content)} 字符）")

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
