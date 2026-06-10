# 0006 tasks —— 提示词模块化装配 + 缓存通道分离 + 动态注入

顺序：可独立验证的（T1/T2）靠前；契约扩展（T4）是缓存（T5/T6）的前置；注入（T3）依赖环境
快照（T2）；末尾两个雷打不动 = T7 接入主流程、T8 端到端验证。

---

## T1｜拆分系统提示词为命名模块 + 有序拼接

- **影响文件**：`coreagent/prompts.py`
- **依赖**：无
- **参考定位**：现 `SYSTEM_PROMPT_BASE`、`build_system_prompt()`
- 做什么：把单条 base 拆成 7 个命名职责常量（身份 / 行为 / 工具使用 / 代码规范 / 安全边界 /
  任务模式 / 输出风格），按固定优先级放进一个有序列表，`build_system_prompt` 改为遍历该列表拼接，
  仍输出「稳定系统文本」并附工具清单。把双重强化规则文案（见 checklist 固定值）写进
  「工具使用 / 代码规范」模块。**不引入** PromptModule / priority / 适用判定抽象。

## T2｜环境快照采集与格式化

- **影响文件**：`coreagent/prompts.py`（或新增 `coreagent/environment.py`）
- **依赖**：无
- **参考定位**：`coreagent/main.py: main()`（启动装配处）
- 做什么：启动时采集 cwd / OS / 日期 / git 分支 + 简短状态，缓存为只读快照（git 子进程**只调一次**）；
  格式化为带特殊标签的环境块文本。git 不可用时降级（标注「非 git 仓库 / 不可用」），不抛错。

## T3｜动态注入供给（模式提醒 + 前向钩子）

- **影响文件**：新增 `coreagent/injection.py`（或并入 `prompts.py`）
- **依赖**：T2
- **参考定位**：`coreagent/agent.py: run_agent_turn()` 的 `plan_only` / `round_index`
- 做什么：一个供给入口，按（`plan_only`、轮序）产出带特殊标签的临时块。plan 提醒文案传达
  「plan 模式 / 继续提议写操作 / 会被记录为计划、不执行」；节奏 = 首轮全量、其余轮次精简（两档，
  **不**做间隔 N 轮全量）。预留「外部工具上线 / 温和提示」前向钩子（空实现或占位常量，不接 MCP）。

## T4｜provider 契约扩展：结构化 system + 稳定前缀 cache_control

- **影响文件**：`coreagent/providers/base.py`、`coreagent/providers/anthropic.py`、
  `coreagent/providers/openai.py`
- **依赖**：无（可与 T1 并行）
- **参考定位**：`BaseProvider.stream_chat()`、`AnthropicProvider.stream_chat()` 里
  `params["system"] / params["tools"]` 装配处；`OpenAIProvider.stream_chat()`
- 做什么：扩展 `stream_chat` 的 `system` 以接受结构化块（保留纯字符串向后兼容）。Anthropic 端把
  `system` 组成 [稳定模块块（打 cache_control）] + [环境快照块（不打）]，并在 `tools` 末尾按需打
  cache_control。OpenAI 端**行为不变**：仍把 system 拼成单条字符串、忽略 tools、不做缓存。

## T5｜滚动历史缓存断点

- **影响文件**：`coreagent/providers/anthropic.py`
- **依赖**：T4
- **参考定位**：`AnthropicProvider.stream_chat()` 的 `params["messages"]` 装配处
- 做什么：每次请求在「最后一条已定型消息」打滚动 cache_control 断点；动态提醒作为临时标签消息追加在
  该断点**之后**（每请求重建，不写入 `conversation`）。管理断点总数 ≤4（稳定 system + 滚动 messages）。

## T6｜缓存可观测：解析 usage 缓存字段

- **影响文件**：`coreagent/providers/anthropic.py`、`coreagent/providers/base.py`（`StreamChunk` 可选字段）
- **依赖**：T4
- **参考定位**：`AnthropicProvider.stream_chat()` 里 `stream.get_final_message()`；`StreamChunk`
- 做什么：从最终消息 usage 读取 `cache_creation_input_tokens / cache_read_input_tokens`，记录日志并
  按需透出到 DONE chunk；字段缺失则跳过并记一条「端点未回传缓存字段」日志，不抛错。

## T7｜接入主流程

- **影响文件**：`coreagent/main.py`、`coreagent/agent.py`、`coreagent/tui.py`
- **依赖**：T1–T6
- **参考定位**：`main(): build_system_prompt / build_registry`；`run_agent_turn()` 调 provider 处；
  `TUI` 持有 system / plan 状态处
- 做什么：`main` 用新装配构建稳定 system + 环境快照。agent loop 把（轮序、`plan_only`、环境快照、
  动态提醒）经装配层注入到断点之后；确保 env 与 reminder **都不写入** `conversation`（历史仍 append-only）。

## T8｜端到端验证

- **影响文件**：`tests/test_prompts.py`、`tests/test_providers.py`、`tests/test_agent.py`（按需新增）；手动 e2e
- **依赖**：T7
- **参考定位**：checklist 全部条目
- 做什么：单测断言断点落位、env / reminder 不进持久化历史、注入节奏两档、双重强化文案落位、OpenAI 行为不变、
  环境快照只采集一次、#0004 既有测试仍绿。手动 e2e 在 DeepSeek 端点跑通对话 + 一次多轮工具循环，
  记录是否回传缓存字段。
