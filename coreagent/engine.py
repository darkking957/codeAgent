"""profile 驱动的引擎入口（#0019）。

承接 #0018（`run_agent_turn` 已是 yield 类型化事件的 async 生成器、对前端无感知）与
`coreagent.profiles`（`AgentProfile` 把「跑什么」收口）。本模块是一层**薄入口**：接收一个
profile 跑，把 profile 的四字段拆回 `run_agent_turn` 的散参——

  profile.system    → system
  profile.tools     → allow_tools（工具白名单；空集 = 模型不获任何工具）
  profile.workspace → cwd（按调用透传给工具）
  profile.approval  → 合成门禁（命中 → ASK / 走确认；否则 → ALLOW / 直放）

并透传 confirm / cancel，**原样** `async for` 转发 `run_agent_turn` 的 #0018 事件——不复制
循环逻辑、不重塑事件、不新增事件类型。确认怎么满足由消费方决定（CLI 走交互确认、HTTP 自动
放行），入口只声明「命中审批集即需确认」的门禁语义、不关心确认结果如何产生。

本模块**绝不** import 渲染 / 终端 / HTTP 库、**绝不** print（与 profiles / 引擎同保持纯净，
可被任意前端复用）。
"""

from collections.abc import AsyncGenerator

from coreagent.agent import ConfirmCb, GateCb, run_agent_turn
from coreagent.conversation import Conversation
from coreagent.permissions.rules import ALLOW, ASK, Decision
from coreagent.profiles import AgentProfile
from coreagent.tools.registry import ToolRegistry


def _profile_gate(approval: frozenset[str]) -> GateCb:
    """据 profile 审批集合成门禁：工具名命中审批集 → ASK（走确认）；否则 → ALLOW（直放）。

    返回既有 ``Decision``（不另造裁决类型，#0007）；``stage="profile"`` 标明来源便于审计。
    """

    def gate(tool_name: str, tool_input: dict) -> Decision:
        if tool_name in approval:
            return Decision(ASK, stage="profile", reason="profile 声明该工具需审批")
        return Decision(ALLOW, stage="profile")

    return gate


async def run_profile(
    provider,
    profile: AgentProfile,
    conversation: Conversation,
    registry: ToolRegistry,
    *,
    confirm: ConfirmCb | None = None,
    cancel=None,
) -> AsyncGenerator:
    """接收一个 profile 跑：拆字段 → 合成门禁 → 原样转发 `run_agent_turn` 的 #0018 事件。

    - ``confirm``：确认回调（命中审批集的工具执行前调用）。CLI 传交互确认、HTTP 传自动放行；
      入口不关心其实现，只负责把门禁判 ASK 的工具交给它。
    - ``cancel``：可选取消令牌（任何带 ``is_set()`` 的对象），原样透传给引擎。

    入口不复制引擎循环逻辑：仅把 profile 拆成散参并 ``async for`` 透传事件。
    """
    async for event in run_agent_turn(
        provider,
        conversation,
        registry,
        profile.system,
        allow_tools=set(profile.tools),
        cwd=profile.workspace,
        gate=_profile_gate(profile.approval),
        confirm=confirm,
        cancel=cancel,
        # 服务端工具声明（#0024 web_search）：profile 携带、原样透传给引擎追加到工具清单尾部。
        server_tools=list(profile.server_tools) or None,
    ):
        yield event
