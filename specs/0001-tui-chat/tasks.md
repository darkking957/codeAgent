# tasks: TUI 纯对话

## T1｜项目骨架与配置加载

影响文件：`pyproject.toml` / `coreagent/__init__.py` / `coreagent/__main__.py` / `coreagent/config.py` / `config.yaml`

依赖：无

工作：
- 建立包结构与 `pyproject.toml`（依赖：anthropic / openai / rich / prompt-toolkit / pyyaml）
- `Config` 数据类：`protocol` / `model` / `base_url` / `api_key` / `ThinkingConfig`
- `load_config(path)` 从 YAML 加载，支持 `${ENV_VAR}` 占位符解析
- 提供示例 `config.yaml`

---

## T2｜Provider 抽象接口

影响文件：`coreagent/providers/base.py` / `coreagent/providers/__init__.py`

依赖：T1

工作：
- `ChunkType` 枚举：`THINKING` / `TEXT` / `DONE`
- `StreamChunk(type, content)` 数据类
- `BaseProvider` 抽象类：`stream_chat(messages, system=None)` 声明为抽象方法，返回异步生成器
- `create_provider(config)` 工厂函数

---

## T3｜Anthropic Provider

影响文件：`coreagent/providers/anthropic.py`

依赖：T2

工作：
- `AnthropicProvider(config)` 实现 `BaseProvider`
- 使用 `client.messages.stream()` 做 SSE 流式请求
- 监听 `content_block_delta` 事件，区分 `thinking_delta` 与 `text_delta`，分别 yield 对应 `StreamChunk`
- `thinking.enabled=true` 时附加 `thinking` 参数到请求
- 流结束后 yield `DONE`

---

## T4｜OpenAI Provider

影响文件：`coreagent/providers/openai.py`

依赖：T2

工作：
- `OpenAIProvider(config)` 实现 `BaseProvider`
- 使用 `chat.completions.create(stream=True)` 做 SSE 流式请求
- 每个非空 `delta.content` yield `StreamChunk(TEXT, ...)`
- 流结束后 yield `DONE`
- system prompt 插入为第一条 `{"role": "system"}` 消息

---

## T5｜Conversation（历史 + 持久化）

影响文件：`coreagent/conversation.py`

依赖：T1

工作：
- `Conversation` 类，内部维护 `messages: list[dict]`
- `add_user(text)` / `add_assistant(text, thinking="")` —— 有 thinking 时写入内容块格式
- `clear()` / `get_messages()`
- `save(path)` 序列化到 JSON 文件（覆盖写）
- `load(path)` 从 JSON 文件恢复，文件不存在时返回空 `Conversation`
- 默认持久化路径：`~/.config/coreagent/history.json`

---

## T6｜重试包装

影响文件：`coreagent/retry.py`

依赖：T2

工作：
- `stream_with_retry(provider, messages, max_retries=3)` 异步生成器
- 捕获 API 异常，打印"第 N 次重试…"，退避后重试
- 超出次数后将异常向上抛出
- 退避策略：1s / 2s / 4s（指数）

---

## T7｜TUI 主循环

影响文件：`coreagent/tui.py`

依赖：T5 / T6

工作：
- `TUI(provider, config, conversation)` 类
- 用 `prompt_toolkit.PromptSession` 接收用户输入（带历史、支持 Ctrl+C 中断）
- 输入处理：`/clear` / `/exit` / `/quit` / `/help`；其余视为对话消息
- 调用 `stream_with_retry` 获取流，实时打印：
  - `THINKING` chunk：以区分样式（灰色 + 前缀标记）实时流出
  - `TEXT` chunk：正常颜色实时流出
  - `DONE`：换行，调用 `conversation.add_assistant()` + `conversation.save()`
- 欢迎横幅展示当前 model / protocol / thinking 状态

---

## T8｜入口组装

影响文件：`coreagent/main.py`

依赖：T1 / T3 / T4 / T7

工作：
- `main()` 解析 `--config` 参数
- 加载 config → 创建 provider → 加载 conversation（从文件）→ 启动 TUI
- 错误（配置缺失、未知 protocol）打印友好信息后退出

---

## T9｜端到端验证

影响文件：无新文件；验证 checklist.md 中全部端到端条目

依赖：T1–T8

工作：按 checklist.md 逐条运行，贴命令 + 输出作为证据。
