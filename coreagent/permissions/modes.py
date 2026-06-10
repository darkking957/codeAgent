"""五级权限模式（#0007 T5）。

模式只调节默认严格度与自动批准范围，叠加在规则求值之上；deny 规则在任何模式
（除 bypassPermissions）下均不被绕过（见 spec 能力 14）。

  plan          只读探索：写类工具被拦截记为计划项（沿用 #0004 plan-only 行为）。
  default       读放行 + 写审批（默认模式）。
  acceptEdits   文件编辑（write_file/edit_file）自动批准；run_command 仍按规则裁决。
  auto          保留档：分类器落地前等同 default（仅占位，见 spec Out of Scope）。
  bypassPermissions  跳过整条权限层（含 deny / pre-filter）；语义上仅限隔离容器。
  dontAsk       子 Agent 默认模式（#0014）：规则求值**等价 default**（rank=1，allow 放行 /
                deny 拒 / 无规则 ask）；语义是「非交互、不弹窗」——「不弹窗」由子 Agent 的
                非交互确认通道（ask→自动拒绝）实现，**非**由本模式实现。不入 MODES（不对
                主 Agent 的 /mode 暴露），仅在 spawn 子 Agent 时固定指定。

「升档丢弃宽泛规则」：升到 acceptEdits 及以上时，非预置的宽泛 allow 规则（如 `Bash(*)`）
被丢弃，精确规则保留。该过滤是**纯函数式**的（按当前模式即时计算），不重置、不写盘，
故降档后宽泛规则随基线重新生效（见 engine.evaluate）。
"""

PLAN = "plan"
DEFAULT = "default"
ACCEPT_EDITS = "acceptEdits"
AUTO = "auto"
BYPASS = "bypassPermissions"
# 子 Agent 默认非交互模式（#0014）；规则求值等价 default，不入主 Agent 的 /mode 选单。
DONT_ASK = "dontAsk"

# 模式枚举（顺序即展示顺序）；默认模式 = default。dontAsk 是子 Agent 专用、不对主 Agent 暴露，
# 故**不入** MODES（避免出现在 /mode 选单；其合法性由 _RANK 成员资格保证）。
MODES = [PLAN, DEFAULT, ACCEPT_EDITS, AUTO, BYPASS]
DEFAULT_MODE = DEFAULT

# 自治程度排名：越大越自治。auto / dontAsk 等同 default（同级 rank=1）。升档丢弃宽泛规则的
# 阈值 = acceptEdits。dontAsk 同级 default → 规则求值、升档判定、文件编辑豁免均与 default 一致。
_RANK = {PLAN: 0, DEFAULT: 1, AUTO: 1, DONT_ASK: 1, ACCEPT_EDITS: 2, BYPASS: 3}


def normalize_mode(mode: str | None) -> str:
    """非法 / 缺省 / 非字符串模式回落 default（fail-safe，不抛——含 unhashable 入参）。"""
    return mode if isinstance(mode, str) and mode in _RANK else DEFAULT_MODE


def is_plan(mode: str) -> bool:
    return normalize_mode(mode) == PLAN


def is_bypass(mode: str) -> bool:
    return normalize_mode(mode) == BYPASS


def auto_approves_edits(mode: str) -> bool:
    """acceptEdits：write_file/edit_file 命中 ask 时自动批准（不弹确认）。"""
    return normalize_mode(mode) == ACCEPT_EDITS


def is_escalated(mode: str) -> bool:
    """是否处于「升档」（≥ acceptEdits）：此时丢弃非预置的宽泛 allow 规则。"""
    return _RANK[normalize_mode(mode)] >= _RANK[ACCEPT_EDITS]


# ── Shift+Tab 模式循环 + 合法档集（#0017）────────────────────────────────────────
# 循环基序：default → acceptEdits → plan；Max 资格开启时在末尾追加 auto（排 plan 之后）。
# bypassPermissions 永不进循环（仅 /mode 显式切换）；dontAsk 是子 Agent 专用、本就不在此列。
# 全部为纯函数式即时计算：不写盘、不重置规则、不改 _RANK（见 spec 非功能要求）。
_CYCLE_BASE = [DEFAULT, ACCEPT_EDITS, PLAN]


def cycle_modes(max_enabled: bool) -> list[str]:
    """当前 Max 资格下的 Shift+Tab 循环序列；Max 开追加 auto，否则三档。"""
    return [*_CYCLE_BASE, AUTO] if max_enabled else list(_CYCLE_BASE)


def next_mode(current: str, max_enabled: bool) -> str:
    """Shift+Tab 正向循环的下一档：到末尾回绕到起点。

    当前档不在循环序列（bypassPermissions，或 Max 关时的 auto）→ 回起点 default。
    """
    seq = cycle_modes(max_enabled)
    cur = normalize_mode(current)
    if cur not in seq:
        return seq[0]
    return seq[(seq.index(cur) + 1) % len(seq)]


def available_modes(max_enabled: bool) -> list[str]:
    """/mode 可选档集：Max 关时隐去 auto；恒不含 dontAsk（子 Agent 专用、不在 MODES）。"""
    return [m for m in MODES if m != AUTO or max_enabled]
