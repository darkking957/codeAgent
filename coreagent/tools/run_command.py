"""run_command：执行 shell 命令，实时回显输出 + 计时，带超时（需确认）。

经 `/bin/sh -c <command>` 执行：支持管道/重定向等 shell 语义，但以 argv 列表把命令交给
解释器（不经 subprocess 的 shell 开关），避免上层再套一层 shell。

以 Popen + 双 reader 线程逐行抽干 stdout / stderr：运行期间把每行实时回显到注入的 writer
（解决「执行无反馈」），长时间无输出时按心跳间隔打「执行中… Ns」，结束打「完成 / 超时 + 用时」；
同时累积完整输出回灌模型。超时与失败都包成结构化结果，不挂死、不抛裸异常。
"""

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable

from coreagent.tools.base import Tool, ToolResult

# 默认整体超时（秒）：构建 / 测试 / 安装类命令偏长，给足余量；超时文案与生效值保持一致。
DEFAULT_TIMEOUT = 120
# 单条命令超时硬上限（秒）：模型经 timeout 参数最多放宽到此（注册中心兜底守卫须大于本值）。
MAX_TIMEOUT = 600
# 静默心跳间隔（秒）：命令长时间无输出时，每隔该秒数回显一行「执行中… Ns」，让用户看到仍在跑。
_HEARTBEAT_INTERVAL = 5.0
# 进程 wait 的轮询粒度（秒）：兼顾超时判定 / 心跳节奏的响应度与空转开销。
_POLL_INTERVAL = 0.5


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在 shell 中执行命令并返回 stdout / stderr / 退出码；运行期间实时回显输出。"
        "优先使用专用工具（read_file / edit_file / glob / grep）；"
        "run_command 仅用于没有专用工具覆盖的操作（运行测试 / 构建 / git 等）。"
        f"默认超时 {DEFAULT_TIMEOUT} 秒，构建 / 测试等长命令可传 timeout（秒）放宽，最大 {MAX_TIMEOUT}。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {
                "type": "integer",
                "description": (
                    f"本条命令的超时秒数；缺省 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}。"
                    "构建 / 测试 / 安装等长命令按需放宽，避免被默认超时打断。"
                ),
            },
        },
        "required": ["command"],
    }
    requires_confirmation = True

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        writer: Callable[[str], None] | None = None,
    ) -> None:
        self.timeout = timeout
        # 实时回显 sink：接收一行纯文本（不含行尾换行），由 sink 决定缩进 / 着色 / 落地。
        # None = 不回显（单测 / 非交互保持安静、确定性）；TUI 注入指向终端的 writer。
        self._writer = writer

    def set_writer(self, writer: Callable[[str], None] | None) -> None:
        """注入 / 撤下实时回显 sink（TUI 装配后调用，工具本身保持 UI 无关）。"""
        self._writer = writer

    def _emit(self, line: str) -> None:
        """把一行回显交给 sink；sink 自身异常绝不拖垮命令执行。"""
        writer = self._writer
        if writer is None:
            return
        try:
            writer(line)
        except Exception:  # noqa: BLE001 —— 回显失败不影响命令本身
            pass

    def _resolve_timeout(self, arguments: dict) -> int:
        """取本次生效超时：优先模型传入的 timeout，夹到 (0, MAX_TIMEOUT]；非法 / 缺省回落 self.timeout。"""
        raw = arguments.get("timeout")
        if raw is None:
            return self.timeout
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return self.timeout
        if val <= 0:
            return self.timeout
        return min(val, MAX_TIMEOUT)

    def execute(self, arguments: dict) -> ToolResult:
        command = arguments.get("command", "")
        if not command.strip():
            return ToolResult.fail("run_command 失败：command 为空")
        timeout = self._resolve_timeout(arguments)

        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # 行缓冲，配合逐行读实现实时回显
                # 独立进程组：超时时整组 kill，连带 sh 派生的子孙进程（如 sleep），
                # 否则子孙继承管道写端不闭合 → reader 线程读不到 EOF → 收尾 join 卡死。
                start_new_session=True,
            )
        except OSError as e:
            return ToolResult.fail(f"run_command 失败：无法启动命令：{e}")

        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        # 两个 reader 线程分别抽干 stdout / stderr：避免单管道写满阻塞导致死锁，
        # 并保留 stdout / stderr 分离（回灌格式不变）。每读到一行即累积 + 实时回显。
        def _drain(stream, buf: list[str], is_err: bool) -> None:
            try:
                for line in stream:
                    buf.append(line)
                    self._emit(("[stderr] " if is_err else "") + line.rstrip("\n"))
            finally:
                stream.close()

        t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_buf, False), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf, True), daemon=True)
        t_out.start()
        t_err.start()

        start = time.monotonic()
        next_beat = start + _HEARTBEAT_INTERVAL
        timed_out = False
        while True:
            try:
                proc.wait(timeout=_POLL_INTERVAL)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if now - start >= timeout:
                    timed_out = True
                    break
                if now >= next_beat:
                    self._emit(f"… 执行中（{int(now - start)}s）")
                    next_beat = now + _HEARTBEAT_INTERVAL

        if timed_out:
            # 整组 kill（连带子孙）→ 管道写端尽数闭合 → reader 线程迅速读到 EOF 退出。
            self._kill_process_group(proc)
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)
            self._emit(f"✗ 超时：{int(time.monotonic() - start)}s（上限 {timeout}s）")
            # 文案与既有契约保持一致：命令执行超时（超过 N 秒）。
            return ToolResult.fail(f"命令执行超时（超过 {timeout} 秒）")

        # 正常结束：等 reader 线程把剩余输出收尾，拿到完整 stdout / stderr。
        t_out.join()
        t_err.join()
        returncode = proc.returncode
        mark = "✓" if returncode == 0 else "✗"
        self._emit(f"{mark} 完成（exit {returncode}，用时 {time.monotonic() - start:.1f}s）")

        payload = self._format(returncode, "".join(stdout_buf), "".join(stderr_buf))
        return ToolResult(success=(returncode == 0), content=payload)

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        """超时收尾：优先按进程组 SIGKILL（连带 sh 派生的子孙），失败回落单进程 kill，再 reap 防僵尸。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _format(returncode: int, stdout: str, stderr: str) -> str:
        parts = [f"exit_code: {returncode}"]
        if stdout:
            parts.append("stdout:\n" + stdout.rstrip("\n"))
        if stderr:
            parts.append("stderr:\n" + stderr.rstrip("\n"))
        return "\n".join(parts)
