# spec: TUI 纯对话

状态：进行中

> 注：重试行为（能力 8）与若干工程化细节由 #0002 调整加固——不可恢复错误不再重试。以 #0002 为准。

## 背景

CoreAgent 第一个可交互版本。用户在终端启动后进入交互界面，与 LLM 进行多轮对话。本 spec 不涉及 tool use / 文件操作 / Agent 能力，只做纯对话。

## 目标用户

在终端工作的开发者。

## 能力清单

1. 启动后进入交互式终端界面（TUI），可输入文本
2. AI 回复流式逐字打印，不等全部生成完再输出
3. 支持多轮对话，AI 能记住当前会话的历史消息
4. 对话历史持久化到本地 JSON 文件，重启后可恢复上次会话
5. 支持 Anthropic Claude 和 OpenAI 两种 API 后端
6. 通过 YAML 配置文件切换 provider，四个核心字段：`protocol` / `model` / `base_url` / `api_key`
7. 支持 Claude extended thinking，thinking 内容实时流式打印（非事后展示）
8. API 调用失败自动重试，最多 3 次，重试间有退避
9. 内置命令：`/clear`（清空历史）、`/exit`（退出）、`/help`（显示帮助）

## 非功能要求

- Provider 层抽象为统一接口：新增后端只需实现该接口，调用方代码不变
- 流式输出不做额外缓冲，首字节延迟等于实际网络延迟
- 重试对用户可见（打印"第 N 次重试…"）

## 设计骨架

```
coreagent/
├── config.py           加载并解析 YAML 配置
├── conversation.py     对话历史（内存 + JSON 持久化）
├── providers/
│   ├── base.py         BaseProvider 抽象类 + StreamChunk 数据结构
│   ├── anthropic.py    Anthropic 实现（SSE streaming + thinking）
│   └── openai.py       OpenAI 实现（SSE streaming）
├── tui.py              TUI 主循环
└── main.py             入口，组装各模块
```

`StreamChunk` 有三种类型：`THINKING` / `TEXT` / `DONE`。

`BaseProvider.stream_chat(messages)` 返回异步生成器，yield `StreamChunk`。

`Conversation` 负责：
- 追加 user / assistant 消息
- 将 assistant 消息中的 thinking 块原样保留（Anthropic 多轮需要）
- 序列化 / 反序列化到 JSON 文件

## Out of Scope

- Tool use、文件操作、代码编辑
- LangGraph / FastAPI / RAG / MCP（后续 spec 引入）
- 多会话管理、会话切换 UI
- TUI 内 Markdown 渲染
- 流式 thinking 的折叠 / 截断交互
