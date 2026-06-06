"""run_command：执行 shell 命令，带超时（需确认）。

经 `/bin/sh -c <command>` 执行：支持管道/重定向等 shell 语义，但以 argv 列表把命令交给
解释器（不经 subprocess 的 shell 开关），避免上层再套一层 shell。超时与失败都包成结构化
结果，不挂死、不抛裸异常。
"""

import subprocess

from coreagent.tools.base import Tool, ToolResult

# 默认整体超时（秒）。超时文案与本值保持一致（见 checklist 固定值）。
DEFAULT_TIMEOUT = 30


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在 shell 中执行命令并返回 stdout / stderr / 退出码。"
        "优先使用专用工具（read_file / edit_file / glob / grep）；"
        "run_command 仅用于没有专用工具覆盖的操作（运行测试 / 构建 / git 等）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
        },
        "required": ["command"],
    }
    requires_confirmation = True

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    def execute(self, arguments: dict) -> ToolResult:
        command = arguments.get("command", "")
        if not command.strip():
            return ToolResult.fail("run_command 失败：command 为空")
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.fail(f"命令执行超时（超过 {self.timeout} 秒）")
        except OSError as e:
            return ToolResult.fail(f"run_command 失败：无法启动命令：{e}")

        payload = self._format(proc.returncode, proc.stdout, proc.stderr)
        return ToolResult(success=(proc.returncode == 0), content=payload)

    @staticmethod
    def _format(returncode: int, stdout: str, stderr: str) -> str:
        parts = [f"exit_code: {returncode}"]
        if stdout:
            parts.append("stdout:\n" + stdout.rstrip("\n"))
        if stderr:
            parts.append("stderr:\n" + stderr.rstrip("\n"))
        return "\n".join(parts)
