# tasks: 工具系统（#0003）

> 顺序：可独立验证的工具与基建靠前（T1~T7），模型 / 会话对接居中（T8~T9），编排在后（T10），末尾两个雷打不动（T11 接入主流程 / T12 端到端）。
> 每个任务一次会话内可完成。跨功能引用按 ID（#0003）；文件夹内任务用 T1/T2。

## T1｜Tool 接口与结构化结果

影响文件：`coreagent/tools/base.py`（新建）

依赖：无

参考：ABC 风格仿 `coreagent/providers/base.py: BaseProvider`；结果类型 dataclass 风格仿 `StreamChunk`（base.py L13-18）

工作：

- 定义 Tool 抽象：名称、描述、参数 Schema（JSON Schema dict）、执行方法；附「需确认」类属性。
- 定义结构化结果类型：成功 / 失败标志 + 文本载荷（供回灌模型）。
- 仅接口，不含具体工具。

---

## T2｜注册中心 + 统一执行入口

影响文件：`coreagent/tools/registry.py`（新建）

依赖：T1

参考：工厂 / 集中装配风格仿 `coreagent/providers/__init__.py: create_provider`

工作：

- 登记工具、按名查找、转成 Anthropic API 认得的工具清单。
- 统一执行入口：按名分派；整体超时守卫 + 捕获任意异常 → 结构化失败结果（不上抛、不崩溃）。

---

## T3｜读 / 写 / 改文件三工具

影响文件：`coreagent/tools/read_file.py`、`write_file.py`、`edit_file.py`（新建）

依赖：T1

参考：原子写沿用 `coreagent/conversation.py: Conversation.save`（L44-63）临时文件 + `os.replace` 思路

工作：

- read 读全文；文件不存在 → 结构化错误。
- write 覆盖写并建父目录。
- edit 原文唯一匹配替换：0 匹配 / 多匹配各报清楚错误（精确文案见 checklist）。
- write / edit 标「需确认」，read 标「免确认」。

---

## T4｜执行命令工具（超时 + 结构化错误）

影响文件：`coreagent/tools/run_command.py`（新建）

依赖：T1

参考：标准库 `subprocess`；超时语义与 T2 一致

工作：

- 跑 shell 命令，捕获 stdout / stderr / returncode 并合成文本载荷。
- 超时（默认值见 checklist）与执行失败转结构化结果，不挂死、不抛裸异常。
- 标「需确认」。

---

## T5｜glob 工具（pathspec / gitignore）

影响文件：`coreagent/tools/glob_tool.py`（新建）；`pyproject.toml`（`dependencies` 增 `pathspec`）

依赖：T1

参考：`pathlib` / `os.walk`；gitignore 解析交 `pathspec`；运行时依赖位置仿 `pyproject.toml` L6-12

工作：

- 按 glob 模式匹配文件路径。
- 读项目 `.gitignore` 经 pathspec 过滤命中项，并恒排除 `.git`。
- 标「免确认」。

---

## T6｜ripgrep 解析骨架 + grep 工具

影响文件：`coreagent/tools/ripgrep.py`、`coreagent/tools/grep_tool.py`（新建）；`coreagent/bin/`（落 linux-x64 二进制）

依赖：T1

参考：调用恒为 `subprocess.run([RG_PATH, ...], shell=False)`；按 `sys.platform` + `platform.machine()` 解析路径

工作：

- 二进制路径解析：本步仅映射 linux-x64 到已落地二进制；其余平台抛清楚的「未打包」错误（文案见 checklist）。
- grep 由本地拼装 rg 参数（模型只给 pattern / 路径，不给命令字符串）、调用 rg、解析输出为 `file:line` 结果。
- 标「免确认」。

---

## T7｜系统提示词

影响文件：`coreagent/prompts.py`（新建）

依赖：无

工作：

- 精简 system prompt：身份（终端编程助手）、可用工具、需读 / 改文件时调用工具而非臆测。关键语义点见 checklist。

---

## T8｜Provider 工具支持（契约扩展 + 流式工具调用解析）

影响文件：`coreagent/providers/base.py`、`coreagent/providers/anthropic.py`；`tests/conftest.py`（假 provider 的 `stream_chat` 补 `tools` 入参）

依赖：T2

参考：`anthropic.py: stream_chat`（L17-67）现有事件循环 `content_block_delta`；`StreamChunk`（base.py L13-18）；假事件流风格见 `tests/test_providers.py: _FakeStream` / `_delta_event`（L30-69）

工作：

- `stream_chat` 增 `tools` 入参并传给 Anthropic API。
- 解析 tool_use 流式事件、**拼接参数 JSON 碎片**得完整调用。
- 完成事件携带：已组装的 assistant 内容块（含 tool_use，供多轮）+ 解析后的工具调用列表（供分派）。
- THINKING / TEXT / DONE 与纯对话产出不变；openai provider 接受但忽略 tools（行为不变）。

---

## T9｜Conversation 工具消息块

影响文件：`coreagent/conversation.py`

依赖：T1

参考：`Conversation.add_assistant`（L19-36）现有 blocks 处理；`save` / `load`（L44-86）

工作：

- 支持追加「含 tool_use 的 assistant 消息」与「含 tool_result 的 user 消息」。
- save / load 往返不丢块；纯对话消息格式与既有行为不变。

---

## T10｜单轮工具编排

影响文件：`coreagent/agent.py`（新建）

依赖：T2、T6、T8、T9

参考：回调解耦风格仿 `coreagent/retry.py: stream_with_retry`（confirm / render 走回调）

工作：

- 流式(带工具) → 若有工具调用：逐个处理（需确认者经 confirm 回调，拒绝则合成「已拒绝」结果、不执行）→ registry 执行 → tool_result 回灌历史。
- 回灌后再流式一次取最终自然语言答复 → 停；第二轮若再出工具调用，忽略不执行（不循环）。
- 一个用户输入内最多两次模型调用。

---

## T11｜接入主流程

影响文件：`coreagent/tui.py`、`coreagent/main.py`

依赖：T10、T7

参考：`tui.py: TUI.run` 主循环（L245-361）、`stream_with_retry` 调用（L291-295）；`main.py` 组装段（L46-54）

工作：

- main 注入注册中心 + 系统提示词。
- TUI 提供「执行前确认」UI 与「工具调用 / 结果」渲染回调，主循环改走 agent 编排。
- 纯对话路径（横幅 / 命令 / 流式打印 / 重试提示）不回归。

---

## T12｜端到端验证

影响文件：无（验证）

依赖：T11

工作：按 `checklist.md` 逐条运行，贴命令 + 输出作为证据。重点：真实触发「读文件并据内容作答」（免确认）与「写 / 改文件需确认」两条路径，并确认纯对话仍正常。全绿后 spec 标「已完成」，文件夹挪入 `specs/_done/`。
