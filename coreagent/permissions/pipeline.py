"""授权流水线编排 + pre-filter 黑名单（#0007 T4）。

每次工具调用按固定顺序经过：
  pre-filter 黑名单 → 规则引擎 → 分类器（默认 auto 分类器，#0017）
任一阶段判 deny 立即短路（不进下一阶段）。bypassPermissions 模式整条跳过（含 deny）。
分类器仅在 mode==auto 时改变裁决（高风险降人工确认 / 安全默认 ask 抬为放行），其余模式直通。

pre-filter：硬编码高危操作直接 deny，不进规则层、不可被任何 allow 规则放行（spec 能力 2）。
本期黑名单针对 run_command 的命令字符串（高危破坏多以 shell 命令形态出现）；这是纵深防御
的第一层，OS 内核沙箱是另一层（见 spec Out of Scope）。
"""

import logging
import re
from collections.abc import Callable
from pathlib import Path

from coreagent.permissions import modes
from coreagent.permissions.audit import AuditLog
from coreagent.permissions.engine import PermissionEngine
from coreagent.permissions.rules import (
    ALLOW,
    ASK,
    DENY,
    Decision,
    rule_for_tool_call,
    split_command,
)
from coreagent.permissions.scopes import local_settings_path, persist_allow_rule

logger = logging.getLogger(__name__)

# ── pre-filter 黑名单。整条命令级模式（fork 炸弹 / mkfs / dd / 重定向裸设备）：在整条命令上搜索。──
# 裸块设备路径（含 device-mapper / by-id / md / loop，不止 sd/nvme）。
_DEVICE = r"/dev/(?:sd|nvme|hd|vd|mmcblk|xvd|mapper/|disk/|md|loop)"
_WHOLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # fork 炸弹 :(){ :|:& };:（容忍 token 间空格变体）。
    (re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:.*&.*\}\s*;\s*:"), "fork 炸弹"),
    # 格式化文件系统 mkfs / mkfs.ext4 ...
    (re.compile(r"\bmkfs(\.\w+)?\b"), "格式化文件系统 mkfs"),
    # dd 写裸块设备 of=/dev/...
    (re.compile(rf"\bdd\b[^\n]*\bof=\s*['\"]?{_DEVICE}"), "dd 写裸块设备"),
    # 重定向写裸块设备 > /dev/...
    (re.compile(rf">\s*['\"]?{_DEVICE}"), "写裸块设备"),
]

# rm 递归删除（-r / -R / -rf / -fr / --recursive，标志可分离、长短皆可）。
_RM_RECURSIVE = re.compile(r"\brm\b(?=.*(?:-\w*[rR]|--recursive))", re.I | re.S)
# 灾难性目标：根 / 家目录 / 裸系统顶层目录（不含其子路径——子目录删除交规则层 + 确认）。
_RM_CATASTROPHIC_TARGET = re.compile(
    r"""(?:^|\s)['"]?(
        /|/\*|~|~/|~/\*|
        /(?:home|etc|usr|var|bin|sbin|lib|lib64|boot|dev|root|sys|proc|opt)/?
    )['"]?(?:\s|$)""",
    re.X,
)


def _is_dangerous_rm(sub: str) -> bool:
    """子命令是否为「递归删除根 / 家 / 系统顶层目录」。"""
    return bool(_RM_RECURSIVE.search(sub) and _RM_CATASTROPHIC_TARGET.search(sub))


def prefilter(tool_name: str, tool_input: dict) -> Decision | None:
    """pre-filter 黑名单：命中返回 deny Decision（stage=prefilter），否则 None（放行进下一阶段）。"""
    if tool_name != "run_command":
        return None
    if not isinstance(tool_input, dict):
        return None
    command = str(tool_input.get("command") or "")
    if not command.strip():
        return None

    def deny(label: str) -> Decision:
        return Decision(DENY, stage="prefilter", matched_rule=label,
                        reason=f"pre-filter 黑名单拦截高危操作：{label}")

    # 整条命令级模式（链式中任意位置出现即拦）。
    for pat, label in _WHOLE_PATTERNS:
        if pat.search(command):
            return deny(label)
    # rm 灾难删除：逐子命令检查（避免跨子命令误配标志与目标；链式中的 rm 也能拦）。
    for sub in split_command(command):
        if _is_dangerous_rm(sub):
            return deny("rm 递归删除根/家/系统目录")
    return None


# ── auto 高风险识别（#0017）。与 pre-filter 黑名单**分层**：pre-filter 对毁灭级操作恒 deny ──
# （不受模式影响、已在前阶段短路）；这里是「正常时自动放行、但风险够高需转人工确认」的一层，
# 仅在 mode==auto 时改变裁决。沿用 pre-filter 的命令级正则范式，作用于整条 run_command 命令串
# （含链式子命令——整条命令上搜索，宁可多触发确认，是授权安全方向）。
_HIGH_RISK_PATTERNS: list[tuple[re.Pattern, str]] = [
    # curl/wget 下载管道直接喂给 shell 执行（curl x.sh | bash / wget -qO- x | sh）。
    (re.compile(r"\b(?:curl|wget)\b.*\|\s*(?:sudo\s+)?\w*sh\b", re.I | re.S), "curl 管道执行"),
    # git 强制推送（--force / -f / +refspec）。
    (re.compile(r"\bgit\s+push\b.*(?:--force\b|-\w*f\b|\s\+\S)", re.I | re.S), "git 强制推送"),
    # rm 递归删除（pre-filter 未拦的非系统目录，如 ./build / node_modules）；沿用 pre-filter 的标志范式。
    (re.compile(r"\brm\b(?=.*(?:-\w*[rR]|--recursive))", re.I | re.S), "rm 递归删除"),
    # 部署类：k8s / helm / terraform apply|destroy / docker push / 任意含 deploy 词。
    (re.compile(r"\bkubectl\s+(?:apply|delete|rollout|create)\b", re.I), "kubectl 部署"),
    (re.compile(r"\bhelm\s+(?:install|upgrade|uninstall)\b", re.I), "helm 部署"),
    (re.compile(r"\bterraform\s+(?:apply|destroy)\b", re.I), "terraform 部署"),
    (re.compile(r"\bdocker\s+push\b", re.I), "docker push"),
    (re.compile(r"\bdeploy\b", re.I), "部署"),
    # 提权（sudo / doas）。
    (re.compile(r"\b(?:sudo|doas)\b", re.I), "提权"),
    # dd 写盘（of=/dev/... 已由 pre-filter 恒 deny；这里覆盖一般 dd if=.. of=..）。
    (re.compile(r"\bdd\b\s.*\bof=", re.I | re.S), "dd 写盘"),
    # 放权 777（chmod 777 / chmod -R 777）。
    (re.compile(r"\bchmod\b.*\b777\b", re.I | re.S), "chmod 777 放权"),
    # git 硬重置 / 强制检出。
    (re.compile(r"\bgit\s+reset\b.*--hard", re.I | re.S), "git 硬重置"),
    (re.compile(r"\bgit\s+checkout\b.*(?:--force|-\w*f\b)", re.I | re.S), "git 强制检出"),
    # 写系统目录（cp/mv/tee/install 落 /etc /usr /bin ... 或重定向写入系统目录）。
    (re.compile(r"\b(?:cp|mv|tee|install)\b[^\n]*\s/(?:etc|usr|bin|sbin|lib|lib64|boot|opt|var)/", re.I),
     "写系统目录"),
    (re.compile(r">\s*/(?:etc|usr|bin|sbin|lib|lib64|boot|opt|var)/", re.I), "写系统目录"),
]


def _high_risk_label(tool_name: str, tool_input: dict) -> str | None:
    """auto 高风险识别：命中返回标签，否则 None。仅作用于 run_command 命令串。"""
    if tool_name != "run_command" or not isinstance(tool_input, dict):
        return None
    command = str(tool_input.get("command") or "")
    if not command.strip():
        return None
    for pat, label in _HIGH_RISK_PATTERNS:
        if pat.search(command):
            return label
    return None


def _auto_classifier(decision: Decision, tool_name: str,
                     tool_input: dict, mode: str) -> Decision:
    """默认分类器（#0017）：仅 ``mode == auto`` 改变裁决，其余模式逐字节直通。

    auto 档「自动但有护栏」：
      - 高风险 → 降级为 ASK（HITL 人工确认，**不** allow；deny 已在前阶段短路，不会到这）；
        若高风险却被规则放行（allow），亦降为 ASK——护栏优先于宽泛放行。
      - 否则「默认 ask（matched_rule 为空）」→ 抬为 ALLOW（安全操作自动放行）。
    不绕过 deny（前阶段短路），不覆盖用户**显式 ask 规则**（matched_rule 非空仍按其意图询问）。
    """
    if mode != modes.AUTO:
        return decision
    risk = _high_risk_label(tool_name, tool_input)
    if risk is not None:
        if decision.outcome == ALLOW:
            return Decision(
                ASK, stage="classifier:auto",
                matched_rule=decision.matched_rule, scope=decision.scope,
                reason=f"auto：高风险操作降级为人工确认（{risk}）",
            )
        return decision  # 已是 ask：保持人工确认（不自动放行）
    if decision.outcome == ASK and decision.matched_rule is None:
        return Decision(
            ALLOW, stage="classifier:auto",
            matched_rule=None, scope=decision.scope,
            reason="auto：安全操作自动放行（未命中具名规则的默认 ask）",
        )
    return decision


# 分类器签名：(decision, tool_name, tool_input, mode) -> decision
ClassifierCb = Callable[[Decision, str, dict, str], Decision]
PrefilterCb = Callable[[str, dict], "Decision | None"]


class PermissionPipeline:
    """授权流水线：编排 pre-filter → 规则引擎 → 分类器，叠加模式调节，并落审计。"""

    def __init__(
        self,
        engine: PermissionEngine,
        audit: AuditLog,
        *,
        base_dir: Path,
        prefilter_fn: PrefilterCb = prefilter,
        classifier: ClassifierCb = _auto_classifier,
    ) -> None:
        self._engine = engine
        self._audit = audit
        self._base_dir = Path(base_dir)
        self._prefilter = prefilter_fn
        self._classifier = classifier

    @property
    def default_mode(self) -> str:
        return self._engine.default_mode

    @property
    def merged_rules(self):
        """合并后的规则集（只读，供 /permission 展示规则摘要）。"""
        return self._engine.merged

    @property
    def local_path(self) -> Path:
        return local_settings_path(self._base_dir)

    def decide(self, tool_name: str, tool_input: dict, mode: str) -> Decision:
        """跑完整条流水线得到裁决并落审计；任一阶段 deny 立即短路。"""
        mode = modes.normalize_mode(mode)

        # bypassPermissions：整条权限层跳过（含 deny / pre-filter），日志标注 bypass。
        if modes.is_bypass(mode):
            return self._record(tool_name, tool_input, mode, Decision(
                ALLOW, stage="bypass", reason="bypassPermissions：跳过权限层（仅限隔离容器）"))

        # ① pre-filter 黑名单：命中即短路 deny（不进规则引擎）。
        pre = self._prefilter(tool_name, tool_input)
        if pre is not None:
            return self._record(tool_name, tool_input, mode, pre)

        # ② 规则引擎：deny 命中即短路（不进分类器 / 模式调节）。
        decision = self._engine.evaluate(tool_name, tool_input, mode)
        if decision.outcome == DENY:
            return self._record(tool_name, tool_input, mode, decision)

        # ③ 分类器插槽（默认 auto 分类器；mode != auto 直通，见 _auto_classifier）。
        decision = self._classifier(decision, tool_name, tool_input, mode)

        # ④ 模式调节：acceptEdits 下文件编辑**因 fail-closed 默认 ask** 时自动批准（deny 不受影响；
        #    高作用域显式 ask 规则 matched_rule 非空 → 不绕过，仍按其意图询问）。
        if (modes.auto_approves_edits(mode) and decision.outcome == ASK
                and decision.matched_rule is None
                and tool_name in ("write_file", "edit_file")):
            decision = Decision(
                ALLOW, stage="mode:acceptEdits",
                matched_rule=decision.matched_rule, scope=decision.scope,
                reason="acceptEdits：自动批准文件编辑（未命中规则的默认 ask）",
            )

        return self._record(tool_name, tool_input, mode, decision)

    def _record(self, tool_name: str, tool_input: dict, mode: str,
                decision: Decision) -> Decision:
        self._audit.record(tool_name, tool_input, decision, mode=mode)
        return decision

    def persist_allow(self, tool_name: str, tool_input: dict) -> str:
        """把一次调用持久化为一条 Local allow 规则；返回规则原文。"""
        rule_str = rule_for_tool_call(tool_name, tool_input)
        persist_allow_rule(rule_str, self.local_path)
        return rule_str
