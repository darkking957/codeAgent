"""T1｜实验门控：单一环境变量控制整套团队能力，默认关闭。

仿 #0014 / `coreagent/main.py` 直读 ``os.environ`` 的范式（如 ``COREAGENT_LOG_LEVEL``）。
本期**只读环境变量**——不引用户级配置文件加载器持久开启（见 spec Out of Scope）。

启用判定（checklist 钉死）：环境变量值**非空**且不在 ``{"0","false","no","off"}``（大小写不敏感）
→ 启用；缺失 / 空 / 落入该集 → 关闭。关闭时主循环、工具列表、键位与现状逐字节一致。
"""

import os

from coreagent.teams import constants


def teams_enabled(env: dict | None = None) -> bool:
    """团队功能是否开启。``env`` 缺省读 ``os.environ``（测试可注入临时映射）。"""
    source = env if env is not None else os.environ
    raw = source.get(constants.EXPERIMENTAL_ENV_VAR)
    if not raw:
        return False
    return raw.strip().lower() not in constants.GATING_FALSY
