"""read_file：读取文件全文（免确认）。"""

from pathlib import Path

from coreagent.tools.base import Tool, ToolResult


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取指定路径文件的全部文本内容。需要了解文件内容时调用本工具，不要臆测。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的文件路径"},
        },
        "required": ["path"],
    }
    requires_confirmation = False

    def execute(self, arguments: dict) -> ToolResult:
        path = arguments.get("path", "")
        p = Path(path)
        if not p.exists() or not p.is_file():
            return ToolResult.fail(f"read_file 失败：文件不存在：{path}")
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult.fail(f"read_file 失败：读取出错：{e}")
        return ToolResult.ok(text)
