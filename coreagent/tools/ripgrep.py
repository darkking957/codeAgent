"""按平台解析随包分发的 ripgrep 二进制路径。

本步只落地 linux-x64 一份二进制；其余平台抛清楚的「未打包」错误（文案见 checklist）。
按 `sys.platform` + `platform.machine()` 归一出 `<plat>-<arch>` 键，对应 `bin/<key>/rg`。
新增平台只需把二进制落进对应目录即可被自动启用。
"""

import platform
import sys
from pathlib import Path

# bin/ 与 coreagent/ 同级（coreagent/bin/<key>/rg）。
_BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_PLAT_MAP = {"linux": "linux", "darwin": "darwin", "win32": "windows"}
_ARCH_MAP = {
    "x86_64": "x64",
    "amd64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def detect_platform() -> tuple[str, str]:
    """归一出 (plat, arch)，如 ('linux', 'x64')、('darwin', 'arm64')。"""
    plat = _PLAT_MAP.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = _ARCH_MAP.get(machine, machine)
    return plat, arch


def rg_path() -> Path:
    """返回当前平台的 ripgrep 二进制路径；未打包则抛 RuntimeError。"""
    plat, arch = detect_platform()
    candidate = _BIN_DIR / f"{plat}-{arch}" / "rg"
    if candidate.is_file():
        return candidate
    raise RuntimeError(f"未打包当前平台（{plat}-{arch}）的 ripgrep 二进制")
