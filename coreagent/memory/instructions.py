"""T3/T4｜项目指令文件：四层加载与拼接 + @include 递归展开（限深度、防越界）。

四层（从根到工作目录、越具体越靠后）：
  组织级 /etc/coreagent/COREAGENT.md
  用户级 ~/.coreagent/COREAGENT.md
  项目级 ./COREAGENT.md 或 ./.coreagent/COREAGENT.md（取首个存在者）
  本地级 ./COREAGENT.local.md

@include 安全（spec：不得读出项目目录之外）：
  - 深度上限 = config.memory.include_max_depth（默认 4），第 max+1 层不再展开并报错；
  - 解析后的 include 路径必须落在项目根之内（统一边界），越界即拒、不读外部内容。

失败软化（spec）：单层 / 单次 include 出错记中文告警并降级（跳过该层），不中断主流程。
expand_includes 本身按设计**抛** InstructionError（供调用方按需处理 / 单测断言文案）；
build_instructions_block 在装配层逐层兜底。
"""

import logging
from pathlib import Path

from coreagent.memory import constants

logger = logging.getLogger(__name__)


class InstructionError(Exception):
    """指令文件展开错误（深度超限 / 路径越界）；文案精确，供单测断言。"""


def _layer_paths(project_dir: Path | str) -> list[tuple[str, Path]]:
    """按拼接顺序返回（层名, 路径）：组织 → 用户 → 项目 → 本地。仅含存在的文件。"""
    project_dir = Path(project_dir)
    layers: list[tuple[str, Path]] = [
        ("组织级", constants.ORG_INSTRUCTION_PATH),
        ("用户级", constants.USER_INSTRUCTION_PATH),
    ]
    # 项目级：优先 ./COREAGENT.md，否则 ./.coreagent/COREAGENT.md（取首个存在者）。
    for rel in constants.PROJECT_INSTRUCTION_RELS:
        candidate = project_dir / rel
        if candidate.exists():
            layers.append(("项目级", candidate))
            break
    layers.append(("本地级", project_dir / constants.LOCAL_INSTRUCTION_FILENAME))
    return [(name, p) for name, p in layers if p.exists() and p.is_file()]


def _is_within(path: Path, root: Path) -> bool:
    """path（已 resolve）是否落在 root（已 resolve）之内（含 root 自身）。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def expand_includes(
    text: str,
    *,
    base_dir: Path,
    project_root: Path,
    max_depth: int,
    _depth: int = 0,
) -> str:
    """递归展开 text 中的 `@include <相对路径>` 指令。

    - 路径相对 base_dir（当前文件所在目录）解析；必须落在 project_root 之内，否则抛越界错误。
    - 每下钻一层 _depth+1；将超过 max_depth 即抛深度超限错误（第 max_depth+1 层不展开）。
    - 指令行整行被替换为被包含文件展开后的内容；非指令行原样保留。
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(constants.INCLUDE_DIRECTIVE + " "):
            out_lines.append(line)
            continue
        rel = stripped[len(constants.INCLUDE_DIRECTIVE):].strip()
        target = (base_dir / rel)
        # 先判越界（即便深度也超，也优先报越界——不读外部内容）。
        if not _is_within(target, project_root):
            raise InstructionError(
                constants.INCLUDE_ESCAPE_ERROR.format(path=rel)
            )
        if _depth + 1 > max_depth:
            raise InstructionError(
                constants.INCLUDE_DEPTH_ERROR.format(max_depth=max_depth, path=rel)
            )
        included = target.read_text(encoding="utf-8")
        out_lines.append(
            expand_includes(
                included,
                base_dir=target.parent,
                project_root=project_root,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        )
    return "\n".join(out_lines)


def build_instructions_block(project_dir: Path | str, *, max_depth: int) -> str:
    """加载四层指令、各层展开 @include、按序拼接，包成带标签的指令块文本。

    无任一层 → 返回空串（调用方据此不注入）。单层失败（读失败 / include 出错）→ 记告警、
    跳过该层（失败软化），不影响其它层与主流程。
    """
    project_root = Path(project_dir)
    parts: list[str] = []
    for name, path in _layer_paths(project_root):
        try:
            raw = path.read_text(encoding="utf-8")
            expanded = expand_includes(
                raw,
                base_dir=path.parent,
                project_root=project_root,
                max_depth=max_depth,
            )
            if expanded.strip():
                parts.append(expanded.rstrip())
        except (InstructionError, OSError, UnicodeDecodeError) as e:
            logger.warning("项目指令文件加载失败，已跳过该层（%s %s）：%s", name, path, e)
    if not parts:
        return ""
    body = "\n\n".join(parts)
    return f"{constants.INSTRUCTIONS_TAG_OPEN}\n{body}\n{constants.INSTRUCTIONS_TAG_CLOSE}"
