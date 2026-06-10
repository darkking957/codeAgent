"""T4｜目录型技能的专属工具加载（进程内 importlib，非 MCP 子进程）。

把目录型技能内的 impl.py 经 importlib 进程内加载、收集其中定义的 Tool 子类、实例化并校验
（name 非空、parameters 为 JSON Schema 对象）。任何加载 / 实例化失败 → 记中文告警并跳过对应
工具，不抛、不崩（延续「解析失败跳过、不阻断整体」）。

信任边界：impl.py 在主进程内任意执行、无沙箱——谁把脚本放进技能目录谁负责（见 spec 非功能要求）。
执行期健壮性由既有 registry.execute 兜底（任何 execute 异常 → ToolResult.fail），本层只管「加载 + 校验」。
"""

import importlib.util
import inspect
import logging
from itertools import count
from pathlib import Path

from coreagent.skills import constants
from coreagent.tools.base import Tool

logger = logging.getLogger(__name__)

# 模块名单调计数：每次加载用唯一模块名，避免 sys.modules 复用导致热更新读到旧代码。
_loader_seq = count()


def _valid_schema(params) -> bool:
    """parameters 须为 JSON Schema 对象（dict 且 type == object）。"""
    return isinstance(params, dict) and params.get("type") == "object"


def _instantiate(cls) -> Tool | None:
    """实例化一个 Tool 子类并校验 name / parameters；不合法 → 记告警返回 None。"""
    try:
        tool = cls()
    except Exception as e:  # noqa: BLE001 —— 构造异常不崩，跳过该工具
        logger.warning("专属工具实例化失败（%s）：%r", getattr(cls, "__name__", cls), e)
        return None
    name = getattr(tool, "name", "")
    if not isinstance(name, str) or not name.strip():
        logger.warning("专属工具缺少合法 name，已跳过：%s", getattr(cls, "__name__", cls))
        return None
    if not _valid_schema(getattr(tool, "parameters", None)):
        logger.warning("专属工具 %s 的 parameters 非法（须为 type=object 的 JSON Schema），已跳过", name)
        return None
    return tool


def load_dedicated_tools(skill_dir: Path) -> list[Tool]:
    """加载 skill_dir/impl.py 中定义的全部 Tool 子类实例（按定义顺序）；无 impl.py → 空列表。

    只收集**本模块内定义**的 Tool 子类（排除从别处 import 进来的 Tool 基类与第三方类），
    避免把基类或外部工具误当专属工具。
    """
    impl = skill_dir / constants.SKILL_IMPL_FILENAME
    if not impl.exists() or not impl.is_file():
        return []

    mod_name = f"coreagent_skill_impl_{skill_dir.name}_{next(_loader_seq)}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(impl))
        if spec is None or spec.loader is None:
            logger.warning("无法为专属工具脚本创建模块规格：%s", impl)
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 —— 脚本顶层执行异常不崩，整脚本跳过
        logger.warning("专属工具脚本加载失败，已跳过整脚本（%s）：%r", impl, e)
        return []

    tools: list[Tool] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        # 只取本模块内定义、且是 Tool 真子类（非 Tool 本身）。
        if obj.__module__ != mod_name:
            continue
        if not issubclass(obj, Tool) or obj is Tool:
            continue
        tool = _instantiate(obj)
        if tool is not None:
            tools.append(tool)
    return tools
