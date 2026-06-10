"""T6｜子 Agent runner：跑到底（复用 #0004 Agent Loop）+ 非交互权限 + 用量回收。

主结构仿 #0012 独立模式 runner（`skills.independent.run_independent_skill`）：起子对话、
`async for chunk in run_agent_turn(...)` 跑到自然结束、取**末轮** TEXT 拼摘要。区别：
- 工具白名单 / 权限模式 / 模型 / 最大轮次来自子 Agent 运行时（角色或 Fork 缺省）；
- **非交互权限**：gate 把任何 ASK 裁决 fail-closed 转 DENY 并附结构化说明（为何被拒 + 如何放开 +
  当前权限模式），confirm 恒拒绝兜底——故全程**绝不**触发交互式确认 UI；显式 allow/deny 规则照常；
- **不改全局** `REJECTED_MESSAGE`：结构化文案经 gate 的 DENY.reason 走 `_deny_feedback` 回灌；
- 用量回收：消费 DONE chunk 的 usage 字段累加为本子 Agent 的独立 token 计数。
"""

import logging
from dataclasses import dataclass

from coreagent.agent import MAX_ROUNDS, ConfirmDecision, run_agent_turn
from coreagent.agents import constants
from coreagent.agents.runtime import SubAgentRuntime
from coreagent.events import RunDone, RunError, TextDelta, TextKind, TurnStart
from coreagent.permissions.rules import ASK, DENY, Decision

logger = logging.getLogger(__name__)


@dataclass
class SubAgentResult:
    """一次子 Agent 执行的结果（前台直接回 ToolResult；后台入后缀通道）。"""

    agent_id: str
    summary: str
    usage_input: int = 0
    usage_output: int = 0
    exit_reason: str | None = None     # complete / max_turns / cancelled
    error: str | None = None           # 不可恢复错误类型名（None = 正常）

    @property
    def ok(self) -> bool:
        return self.error is None


def _make_noninteractive_gate(pipeline, mode: str):
    """子 Agent 门禁：跑流水线得裁决；ASK → fail-closed 转 DENY（附结构化说明）。allow/deny 透传。"""
    def gate(tool_name: str, tool_input: dict) -> Decision:
        decision = pipeline.decide(tool_name, tool_input, mode)
        if decision.outcome == ASK:
            return Decision(
                DENY,
                stage="subagent-noninteractive",
                matched_rule=decision.matched_rule,
                scope=decision.scope,
                reason=(
                    f"子 Agent 为非交互执行（当前权限模式={mode}），撞到需用户确认的工具"
                    f"「{tool_name}」，已 fail-closed 自动拒绝。放开方式：为该工具/命令添加 allow "
                    f"权限规则，或以更高权限模式（如 acceptEdits / bypassPermissions）启动该角色。"
                ),
            )
        return decision
    return gate


async def _reject_confirm(_tool_call: dict) -> ConfirmDecision:
    """非交互兜底确认：恒拒绝。理论上 gate 已把 ASK 转 DENY、不会触达此处；置此确保绝不弹交互 UI。"""
    return ConfirmDecision.REJECT


def _isolation_task_note(cwd: str) -> str:
    """隔离子 Agent（#0015）的工作目录说明：注入到任务前，强调用绝对路径、勿越界。"""
    return (
        f"【隔离工作区】你的工作目录是一个独立的 git worktree：{cwd}\n"
        f"所有文件读写一律使用该目录下的**绝对路径**（以 {cwd} 开头），"
        f"不要使用相对路径，也不要操作该目录之外的文件。"
    )


async def run_subagent(
    runtime: SubAgentRuntime,
    *,
    provider,
    registry,
    system: str | None,
    task: str,
    on_chunk=None,
) -> SubAgentResult:
    """跑一个子 Agent 到自然结束，取末轮收尾文字为摘要返回。

    ``system``：定义式传角色系统提示（role.body）；Fork 式传父 system_prompt。
    ``task``：本次委派的任务指令——追加为对话**末条** user 消息（Fork 式即落在滚动缓存断点之后）。
    隔离子 Agent（#0015，``runtime.worktree`` 非空）在任务前注入工作目录说明（强调用绝对路径）。
    """
    if runtime.worktree is not None:
        task = _isolation_task_note(runtime.cwd) + "\n\n" + task
    runtime.conversation.add_user(task)
    gate = _make_noninteractive_gate(runtime.pipeline, runtime.permission_mode)
    max_rounds = runtime.max_rounds if runtime.max_rounds is not None else MAX_ROUNDS

    last_round_text: list[str] = []
    usage_in = 0
    usage_out = 0
    exit_reason: str | None = None
    error: str | None = None

    try:
        async for ev in run_agent_turn(
            provider,
            runtime.conversation,
            registry,
            system,
            gate=gate,
            confirm=_reject_confirm,
            allow_tools=runtime.allow_tools,
            model_override=runtime.model_override,
            cwd=runtime.cwd or None,
            hooks=runtime.hook_runtime,
            context=runtime.context,
            max_rounds=max_rounds,
        ):
            if isinstance(ev, TurnStart):
                last_round_text = []           # 每轮开始清空：循环结束时即末轮收尾文字
            elif isinstance(ev, TextDelta) and ev.kind is TextKind.ANSWER:
                last_round_text.append(ev.text)
            elif isinstance(ev, RunDone):
                # 终止事件携带**累计** usage（各轮之和）：直接读总量，等价于原逐轮 += 之和。
                exit_reason = ev.reason.value
                usage_in = ev.input_tokens
                usage_out = ev.output_tokens
            elif isinstance(ev, RunError):
                error = ev.error_type
            if on_chunk is not None:
                on_chunk(ev)
    except Exception as e:  # noqa: BLE001 —— 子 Agent 任何异常都不许冒泡崩主循环（真并行无崩溃）
        logger.warning("子 Agent %s 执行异常：%r", runtime.agent_id, e)
        error = type(e).__name__
    finally:
        # 隔离工作树收尾（#0015）：正常 / 取消 / 轮次耗尽 / 报错一视同仁——有改动自动提交并保留、
        # 无改动清掉。失败软化（不丢工作、不崩）。
        if runtime.worktree is not None and runtime.worktree_manager is not None:
            try:
                runtime.worktree_manager.finalize(runtime.worktree)
            except Exception as e:  # noqa: BLE001 —— 收尾失败不丢工作、不崩
                logger.warning("子 Agent %s 工作树收尾失败：%r", runtime.agent_id, e)

    summary = "".join(last_round_text).strip() or constants.EMPTY_SUMMARY_PLACEHOLDER
    return SubAgentResult(
        agent_id=runtime.agent_id,
        summary=summary,
        usage_input=usage_in,
        usage_output=usage_out,
        exit_reason=exit_reason,
        error=error,
    )
