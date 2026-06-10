"""启用级安全门：项目级 server 首次启用确认 + 持久化（#0008 T8）。

安全门两层之一（启用级）：**仅项目级（可提交）** server 在首次启用前需用户确认——因为它来自
仓库里可被任意提交者写入的配置，启动它即等于运行外部进程。用户级 / 本地级 server 视作已信任
（用户自己机器上的配置），不弹。

批准结果持久化到 **local 作用域**的 ``enabledMcpServers`` 键（复用 #0007 的原子写），下次启动
该 server 已在集合内即不再问。拒绝则不连接该 server。

注意：另一层（调用级）由适配器 ``requires_confirmation=True`` + agent loop 既有写类确认承担，
不在本模块。
"""

import json
import logging
from pathlib import Path

from coreagent.mcp import constants
from coreagent.mcp.config import McpServerConfig
from coreagent.mcp.errors import McpError
from coreagent.permissions.scopes import (
    SCOPE_PROJECT,
    _atomic_write_json,
    local_settings_path,
)

logger = logging.getLogger(__name__)

# 项目级首次启用确认提示文案（见 checklist 固定值，精确；含 server 名与来源）。
ENABLE_PROMPT = (
    "检测到项目级 MCP server「{name}」（来源：项目配置，可提交）。"
    "首次启用需确认——是否信任并启动？"
)


def load_enabled(base_dir: Path | str) -> set[str]:
    """读 local 作用域 ``enabledMcpServers`` 集合；缺文件 / 坏文件 → 空集（不崩）。"""
    path = local_settings_path(Path(base_dir))
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return set()
        names = data.get(constants.ENABLED_KEY) or []
        if not isinstance(names, list):
            return set()
        return {str(n) for n in names}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取 %s 的 %s 失败：%s；按空集处理", path, constants.ENABLED_KEY, e)
        return set()


def needs_enable_confirmation(cfg: McpServerConfig, enabled: set[str]) -> bool:
    """是否需要弹首次启用确认：仅「来源 project 且不在已启用集合」才需要。"""
    return cfg.scope == SCOPE_PROJECT and cfg.name not in enabled


def persist_enabled(name: str, base_dir: Path | str) -> None:
    """把 server 标识写入 local 作用域 ``enabledMcpServers``（读旧 → 去重追加 → 原子写）。

    保留文件内其它键（如 #0007 的 permissions），现有文件损坏则不覆盖（保留待修复）。
    """
    path = local_settings_path(Path(base_dir))
    data: dict = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("顶层应为 JSON 对象")
            data = loaded
        except (json.JSONDecodeError, OSError, ValueError) as e:
            raise McpError(
                f"无法写入 {constants.ENABLED_KEY}：现有文件 {path} 解析失败（{e}）；"
                "已保留原文件，请先修复后重试"
            ) from e
    names = data.setdefault(constants.ENABLED_KEY, [])
    if not isinstance(names, list):
        names = data[constants.ENABLED_KEY] = []
    if name not in names:
        names.append(name)
    _atomic_write_json(path, data)
