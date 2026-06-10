"""执行级 OS 沙箱（#0025 T7）：把 run_command / grep 的子进程关进本会话 workspace。

bubblewrap（``bwrap``）构造一个最小命名空间运行子进程：

- **断网**：``--unshare-net``（隔离网络命名空间，仅本地回环且不通）。
- **只读系统**：``--ro-bind / /`` 整树只读，再把**本会话 workspace** ``--bind`` 回来作唯一可写区。
- **不越界 / 跨用户零可见**：``--tmpfs <workspace 根>`` 把所有用户 / 会话目录**覆盖成空 tmpfs**，
  再 ``--bind`` 回**本会话**子目录——别的用户 / 会话目录在沙箱内**根本不存在**（既不可写也不可读）。
- **不继承密钥环境**：``--clearenv`` 清空全部环境变量，只回填 ``PATH`` / ``HOME`` / ``TERM``——
  ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` 等绝不进沙箱。
- **资源限额**：子进程 ``preexec_fn`` 里 ``setrlimit``（CPU / 文件大小 / 句柄数 / 进程数）。
- **fail-closed**：探测到 ``bwrap`` 不可用（无二进制 / 无权限 / 探测命令失败）→ ``wrap`` 抛
  ``SandboxUnavailable``，工具据此返回明确「沙箱不可用」错误，**绝不**无沙箱执行。

经 ``make_web_policy(...)`` 造一个 ``ExecPolicy`` 注入 #0025 的执行策略通道（``tools.exec_policy``）；
web 多用户路径 ``use_policy`` 开、CLI 不设——工具只认通道里的 ``wrap`` 回调，**不** import 本模块
（守层级：工具是核心层，沙箱构造是 web 层）。
"""

import logging
import os
import resource
import shutil
import subprocess

from coreagent.tools.exec_policy import ExecPolicy, SandboxUnavailable, WrappedExec

logger = logging.getLogger("coreagent.web.sandbox")

# 沙箱不可用错误文案的**必含子串**（checklist 钉死：文案含「沙箱不可用」）。
SANDBOX_UNAVAILABLE = "沙箱不可用"

# 子进程资源限额（rlimit）：CPU 秒 / 单文件字节 / 句柄数 / 进程数。AS（地址空间）刻意不限，
# 避免 git / 编译器等 mmap 重的工具误伤——以 CPU + FSIZE + NOFILE + NPROC 覆盖「资源有界」。
RLIMIT_CPU_SEC = 600
RLIMIT_FSIZE_BYTES = 512 * 1024 * 1024
RLIMIT_NOFILE = 4096
RLIMIT_NPROC = 512

# 沙箱内回填的最小环境（clearenv 之后）。
_SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_probe_cache: bool | None = None


def _disabled_by_env() -> bool:
    """测试 / 闭环用开关：置 ``COREAGENT_DISABLE_SANDBOX=1`` 模拟「沙箱不可用」以验 fail-closed。

    注意：它**模拟不可用**（→ fail-closed 拒绝子进程类工具），**不是**「关掉沙箱直接裸跑」——
    绝不存在「绕过沙箱无防护执行」的开关（守安全边界）。
    """
    return os.environ.get("COREAGENT_DISABLE_SANDBOX", "") == "1"


def bwrap_available() -> bool:
    """探测 bubblewrap 是否真的可用（有二进制 + 能起一个带 ``--unshare-net`` 的最小沙箱）。

    结果缓存（探测有成本）；``COREAGENT_DISABLE_SANDBOX=1`` 时恒判不可用（验 fail-closed）。
    """
    if _disabled_by_env():
        return False
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache
    path = shutil.which("bwrap")
    if not path:
        _probe_cache = False
        return False
    try:
        r = subprocess.run(
            [path, "--ro-bind", "/", "/", "--unshare-net", "--die-with-parent", "/bin/true"],
            capture_output=True,
            timeout=10,
        )
        _probe_cache = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _probe_cache = False
    return _probe_cache


def _set_rlimits() -> None:
    """子进程 ``preexec_fn``：设资源限额（失败的单项静默跳过，不拖垮启动）。"""
    for res_id, limit in (
        (resource.RLIMIT_CPU, RLIMIT_CPU_SEC),
        (resource.RLIMIT_FSIZE, RLIMIT_FSIZE_BYTES),
        (resource.RLIMIT_NOFILE, RLIMIT_NOFILE),
        (resource.RLIMIT_NPROC, RLIMIT_NPROC),
    ):
        try:
            resource.setrlimit(res_id, (limit, limit))
        except (ValueError, OSError):
            pass


def build_bwrap_argv(argv: list[str], workspace: str, hide_root: str) -> list[str]:
    """构造 bwrap 包裹命令：断网 / 只读系统 / 仅本会话 workspace 可写 / 清密钥环境。

    ``workspace``：本会话目录（绝对路径，唯一可写区 + chdir）；``hide_root``：要 tmpfs 覆盖的
    用户/会话**总根**（覆盖后再绑回本会话子目录，藏掉别的用户/会话）。
    """
    bwrap = shutil.which("bwrap")
    return [
        bwrap,
        "--ro-bind", "/", "/",            # 整树只读
        "--dev", "/dev",                  # 最小 /dev（含 /dev/null）
        "--proc", "/proc",                # 隔离 pid 后需新挂 /proc
        "--tmpfs", hide_root,             # 覆盖所有用户/会话目录为空 tmpfs（跨用户零可见）
        "--bind", workspace, workspace,   # 仅本会话 workspace 绑回、可写
        "--chdir", workspace,
        "--unshare-net",                  # 断网
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",              # 父（bwrap）死则内部进程随之被杀（配合 killpg 取消/超时）
        "--new-session",
        "--clearenv",                     # 清空环境 → 密钥不进沙箱
        "--setenv", "PATH", _SANDBOX_PATH,
        "--setenv", "HOME", workspace,
        "--setenv", "TERM", "dumb",
        "--",
        *argv,
    ]


def make_web_policy(workspace: str, hide_root: str) -> ExecPolicy:
    """造 web 多用户执行策略：文件类工具约束在 ``workspace`` 子树 + 子进程类工具经 bwrap 沙箱。

    ``wrap`` 闭包在每次子进程启动时探测沙箱可用性：不可用即抛 ``SandboxUnavailable``（fail-closed）。
    """
    ws = os.path.realpath(workspace)
    root = os.path.realpath(hide_root)

    def wrap(argv: list[str], cwd: str | None) -> WrappedExec:
        if not bwrap_available():
            logger.warning("sandbox_unavailable", extra={"event": "sandbox_unavailable", "workspace": ws})
            raise SandboxUnavailable(SANDBOX_UNAVAILABLE)
        return WrappedExec(build_bwrap_argv(argv, ws, root), _set_rlimits)

    return ExecPolicy(confine_root=ws, wrap=wrap)
