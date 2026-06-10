"""grep：调用随包分发的 ripgrep 搜代码内容（免确认）。

固定以「二进制 + 参数列表 + shell=False」调用：rg 参数全由本地代码拼装，
模型只提供 pattern / 路径，不构造命令字符串——从根上规避命令注入。
输出解析为 `file:line` 形式的命中行。
"""

import subprocess

from coreagent.tools.base import PathEscapeError, Tool, ToolResult, resolve_confined
from coreagent.tools.exec_policy import SandboxUnavailable, wrap_subprocess
from coreagent.tools.ripgrep import rg_path

# 单次搜索的整体超时（秒）；注册中心另有更大的兜底守卫。
_TIMEOUT = 30
# 命中行数上限，避免一次回灌淹没上下文。
_MAX_LINES = 200


class GrepTool(Tool):
    name = "grep"
    description = "在代码库中按正则搜索文本内容，返回命中的 file:line 列表。需要查找代码时调用本工具。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "要搜索的正则表达式"},
            "path": {"type": "string", "description": "搜索根目录或文件，默认当前目录"},
        },
        "required": ["pattern"],
    }
    requires_confirmation = False

    def execute(self, arguments: dict, cwd: str | None = None, cancel=None) -> ToolResult:
        pattern = arguments.get("pattern", "")
        # 相对 path 挂到 cwd 下（绝对 path 原样）；cwd=None 时退回进程 cwd（"." → 旧行为）。
        # 约束生效（web 多用户）时 resolve 后须落 workspace 子树，越界即拒（与文件类工具同口径）。
        try:
            path = str(resolve_confined(cwd, arguments.get("path") or "."))
        except PathEscapeError as e:
            return ToolResult.fail(f"grep 失败：{e}")
        if not pattern:
            return ToolResult.fail("grep 失败：pattern 为空")
        try:
            rg = rg_path()
        except RuntimeError as e:
            return ToolResult.fail(f"grep 失败：{e}")

        # 参数列表恒定，shell=False；rg 自带 gitignore 语义与 .git 跳过。
        # pattern / path 仅作为独立 argv 元素传入，不参与任何命令字符串拼接。
        argv = [
            str(rg),
            "--line-number",
            "--no-heading",
            "--with-filename",
            "--color", "never",
            "--",
            pattern,
            path,
        ]
        # 执行级沙箱（#0025）：有活跃策略（web 多用户）→ ripgrep 子进程经 bwrap 沙箱（断网 / 限路径）；
        # 探测不可用即 fail-closed。无策略（CLI）→ 原样、不设 preexec（旧行为不变）。
        try:
            wrapped = wrap_subprocess(argv, cwd)
        except SandboxUnavailable as e:
            return ToolResult.fail(f"grep 失败：{e}")
        try:
            proc = subprocess.run(
                wrapped.argv,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                preexec_fn=wrapped.preexec,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.fail("grep 失败：搜索超时")
        except OSError as e:
            return ToolResult.fail(f"grep 失败：无法启动 ripgrep：{e}")

        # rg 退出码：0=有命中，1=无命中（非错误），≥2=真错误。
        if proc.returncode >= 2:
            return ToolResult.fail(f"grep 失败：{proc.stderr.strip() or 'ripgrep 错误'}")

        lines = [ln for ln in proc.stdout.splitlines() if ln]
        if not lines:
            return ToolResult.ok("（无匹配）")
        truncated = lines[:_MAX_LINES]
        out = "\n".join(truncated)
        if len(lines) > _MAX_LINES:
            out += f"\n…（共 {len(lines)} 行，已截断至 {_MAX_LINES} 行）"
        return ToolResult.ok(out)
