"""Agent Profile：把「这次引擎跑什么」收口成一个显式、不可变的配置（#0019）。

承接 #0018（循环已事件化）：`run_agent_turn` 接收一组散参（system / 工具白名单 / cwd /
门禁）运行、yield 类型化事件、对前端无感知。本模块把这组「跑什么」收口成单一抽象
`AgentProfile`——chat 与 code 不是两个产品，而是同一引擎的两种配置。引擎入口（见
`coreagent.engine.run_profile`）接收一个 profile 跑，把字段拆回 `run_agent_turn` 的散参。

设计（最简优先）：
- 只放现在真用得到的四个字段——系统提示词 / 可用工具名集合 / 工作目录 / 需审批工具名集合；
  不为想象的未来加字段。
- 不引 profile 注册表 / 继承 / 插件机制：新增 profile = 加一个构造函数，不加抽象层。
- 本模块**绝不** import 渲染 / 终端 / HTTP 库、**绝不** print（纯数据 + 构造，可被 CLI /
  HTTP / 测试任意消费方复用）。
"""

from dataclasses import dataclass

from coreagent.prompts import build_system_prompt

# code profile 的六工具全集（即 build_registry 登记的六核心工具名）。
CODE_TOOLS: frozenset[str] = frozenset(
    {"read_file", "write_file", "edit_file", "run_command", "glob", "grep"}
)
# code profile 的需审批集：有副作用的写 / 改 / 执行三者（与只读的 read_file/glob/grep 区分）。
CODE_APPROVAL: frozenset[str] = frozenset({"write_file", "edit_file", "run_command"})

# 服务端 web_search 工具名（#0024）：chat profile 的服务端工具能力标识；与引擎 / 前端共用此名。
WEB_SEARCH_TOOL = "web_search"

# chat profile 的系统提示（无搜索时）：纯文本问答、无工具——刻意不含读写文件 / 执行命令类的工具指引。
CHAT_SYSTEM = """\
你是 CoreAgent 的对话助手（chat 模式），运行在终端里，通过纯文本问答帮助用户。

- 当前会话**无工具**：你不能读取或改动文件、不能执行任何命令，只能基于已有知识与对话\
上下文作答。
- 需要用户那边才有的信息时，直接向用户提问；不要假装查看文件或运行操作，也不要编造\
不存在的内容。
- 用简洁的中文回复，先结论后细节。"""

# chat profile 接入服务端 web_search 时的系统提示（#0024）：声明联网搜索能力 + 标注来源的指引。
CHAT_SYSTEM_SEARCH = """\
你是 CoreAgent 的对话助手（chat 模式），运行在终端里，通过问答帮助用户。

- 你具备**联网搜索**能力（web_search，由服务端执行）：当问题涉及实时信息、训练截止之后的\
事实、或需要核实的具体数据时，**主动调用搜索**，再基于搜索结果作答，并标注来源；不要凭\
记忆编造可能过期的事实。
- 不能读取或改动文件、不能执行命令；需要用户那边才有的信息时直接向用户提问。
- 用简洁的中文回复，先结论后细节。"""


@dataclass(frozen=True)
class AgentProfile:
    """一次引擎运行的不可变配置（#0019）。

    四字段对应「跑什么」：
    - ``system``：系统提示词（交 ``run_agent_turn`` 的 system）；
    - ``tools``：可用工具名集合（→ ``allow_tools`` 白名单；空集 = 模型不获任何工具）；
    - ``workspace``：工作目录（→ ``cwd``，按调用透传给工具；None = 退回进程 cwd）；
    - ``approval``：需审批工具名集合（→ 门禁：命中 ASK、否则 ALLOW，由入口层合成）。

    不可变（frozen + frozenset）：profile 一旦构造即固定，可被并行消费方安全共享。
    """

    system: str
    tools: frozenset[str]
    workspace: str | None
    approval: frozenset[str]
    # 服务端工具声明（#0024 web_search）：每项 ``{"type","name",...}`` **无** input_schema；非本仓
    # ToolRegistry 登记、由端点执行。引擎入口透传给 ``run_agent_turn`` 的 ``server_tools``。缺省空元组
    # （无服务端工具，向后兼容既有四字段构造）。
    server_tools: tuple[dict, ...] = ()


def chat_profile(web_search: dict | None = None) -> AgentProfile:
    """chat 预置：纯问答、空审批集、无工作目录；可选接入服务端 web_search（#0024）。

    ``web_search``：服务端 web_search 工具**声明 dict**（如
    ``{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}``），由 Web 层据
    ``config.web.chat.web_search`` 构造注入。语义：
    - 工具集 ``tools`` **始终含** ``web_search``（服务端工具能力标识；web_search 非 registry 工具，
      故不影响客户端工具裁剪、传给 provider 的客户端工具仍为空）；
    - 审批集 ``approval`` **恒空** → 门禁对 web_search 直放（免审批，视作只读类）；
    - 传入声明 → ``server_tools`` 携带该声明、系统提示切到「有搜索能力」版（端点执行搜索）；
      未传（None）→ ``server_tools`` 为空、系统提示用无工具版（端点不支持时即优雅降级为纯问答）。
    """
    server_tools = (dict(web_search),) if web_search else ()
    return AgentProfile(
        system=CHAT_SYSTEM_SEARCH if server_tools else CHAT_SYSTEM,
        tools=frozenset({WEB_SEARCH_TOOL}),
        workspace=None,
        approval=frozenset(),
        server_tools=server_tools,
    )


def code_profile(
    workspace: str | None = None,
    approval: frozenset[str] | None = None,
) -> AgentProfile:
    """code 预置：六工具全集、编程系统提示、工作目录由入参绑定、审批集 = 写/改/执行三者。

    ``workspace`` 由调用方传入（CLI 用项目根、HTTP 用绑定的真实项目根），便于同一 profile 复用到
    不同工作区；缺省 None → 各工具退回进程 cwd。

    ``approval``（#0023）：审批集覆盖。缺省 None → 用默认 ``CODE_APPROVAL``（写/改/执行三者）；
    传入集合（含空集）→ 整体覆盖，由 Web 配置注入，让「哪些工具需审批」可经配置调整（门禁仍据
    ``profile.approval`` 由引擎入口合成，引擎本身不变）。
    """
    return AgentProfile(
        system=build_system_prompt(sorted(CODE_TOOLS)),
        tools=CODE_TOOLS,
        workspace=workspace,
        approval=CODE_APPROVAL if approval is None else frozenset(approval),
    )
