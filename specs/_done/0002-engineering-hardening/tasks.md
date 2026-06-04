# tasks: 工程化加固

> 顺序：可独立验证的靠前（T1~T2），改造按依赖排（T3~T8），末尾两个雷打不动（T10 接入主流程 / T11 端到端）。

## T1｜工具链与依赖分组

影响文件：`pyproject.toml`

依赖：无

参考：`pyproject.toml` 现有 `[project].dependencies`（L6-12）、`[project.scripts]`（L14-15）

工作：
- `dependencies` 增加运行时依赖 `pydantic>=2`。
- 新增 `[project.optional-dependencies].dev`：pytest / pytest-asyncio / ruff / mypy。
- 新增 `[tool.pytest.ini_options]`（含 asyncio 模式）、`[tool.ruff]`、`[tool.mypy]`（对 rich / prompt_toolkit 等无 stub 的库放宽）。

---

## T2｜领域异常

影响文件：`coreagent/errors.py`（新建）

依赖：无

工作：
- 定义基类 `CoreAgentError`，及 `ConfigError`（配置非法）。
- 仅作分类用，入口据此给出干净退出。

---

## T3｜配置校验（pydantic）

影响文件：`coreagent/config.py`

依赖：T1、T2

参考：现 `Config`/`ThinkingConfig`（L8-20）、`load_config`（L31-55）、`_resolve_env_vars`（L23-28）

工作：
- `Config`/`ThinkingConfig` 改为 pydantic 模型，公开属性名不变（`protocol`/`model`/`api_key`/`base_url`/`thinking`），下游零改动。
- 校验：protocol 合法、model 与 api_key 非空、thinking 预算为正。
- 新增输出上限字段（非思考 / 思考各一，带默认），承接原 provider 中的 magic number。
- `load_config`：先解析 `${ENV}` 再构造模型；pydantic 校验失败统一转 `ConfigError`（中文、指明字段）。
- `create_provider` 对未知 protocol 的兜底 `ValueError` 保留（防御层，#0001 行为不破）。

---

## T4｜原子持久化 + 容错加载

影响文件：`coreagent/conversation.py`

依赖：T2（可选）

参考：`save`（L39-42）、`load`（L44-50）

工作：
- `save`：写同目录临时文件后原子替换，保证中断不损坏既有历史。
- `load`：文件缺失→空历史；非法 JSON / 结构非法→备份损坏文件、空历史启动、记录告警。
- `add_*`/`clear`/`get_messages` 行为与消息格式不变。

---

## T5｜重试分类 + 已输出守卫

影响文件：`coreagent/retry.py`

依赖：无

参考：`stream_with_retry`（L7-19）、退避 `2 ** (attempt-1)`（L10）

工作：
- 新增 `_is_retryable(exc)`：瞬时错误（连接 / 超时 / 限流 / 5xx）可重试；其余不可重试。
- 重试循环：分类判断 + "本轮已 yield 过任何 chunk 则不再重试"守卫；退避 1/2/4s 保持。
- 用户可见性改为 `on_retry` 回调（表现层注入），诊断走 logging；移除直接 `print`。
- 默认 `max_retries=3` 不变。

---

## T6｜providers 去 magic number + 类型

影响文件：`coreagent/providers/anthropic.py`、`coreagent/providers/openai.py`、`coreagent/providers/base.py`

依赖：T3

参考：anthropic `max_tokens`（anthropic.py L14）、thinking 参数（L22-29）；openai `stream_chat`（openai.py L13）

工作：
- Anthropic 输出上限改用 config 字段（替换硬编码，纠正非思考模式笔误值）。
- 补类型标注；`stream_chat` 产出顺序与类型不变（THINKING/TEXT/DONE）。
- OpenAI provider 行为不变（仍不设上限），仅补类型。

---

## T7｜日志配置 + 入口分类退出

影响文件：`coreagent/main.py`

依赖：T2、T3、T5

参考：`main`（L11-33）、错误处理（L16-29）

工作：
- 启动时按环境变量配置 logging（默认安静）。
- 捕获 `ConfigError`/`FileNotFoundError` 等，打印中文友好信息、非零码退出。
- 准备好把 TUI 的"重试提示"接到 `on_retry`（实际接线在 T8/T10）。

---

## T8｜TUI 接入重试回调

影响文件：`coreagent/tui.py`

依赖：T5

参考：`run` 中 `stream_with_retry` 调用（L285-287）、异常展示（L334-342）

工作：
- 向 `stream_with_retry` 传入 `on_retry`，按现有样式打印"第 N 次重试…"。
- 其余主循环、流式打印、命令、横幅、样式全部不变。

---

## T9｜测试套件

影响文件：`tests/`（新建：`test_config.py`/`test_conversation.py`/`test_retry.py`/`test_providers.py`/`conftest.py`）

依赖：T3~T8

工作：
- 假 provider（异步生成器）驱动，**不触网**。
- config：合法加载、`${ENV}` 解析、未知 protocol / 空 api_key / 非正预算 → `ConfigError`、上限默认值。
- conversation：roundtrip、原子写入（无残留临时文件）、损坏 JSON → 备份 + 空历史、缺失文件 → 空。
- retry：成功不重试、可重试错误退避 1/2/4 且 `on_retry` 按序回调、不可重试立即失败、已 yield 后不重试。
- providers：`create_provider` 选型、未知 protocol 抛 `ValueError`、stream 产出顺序。

---

## T10｜接入主流程

影响文件：无新文件（验证装配）

依赖：T1~T9

工作：
- `coreagent` CLI 正常启动并展示横幅；`/help` `/clear` `/exit` 与空输入路径无异常。
- 重试提示在界面可见（用假 provider 触发可重试错误）。

---

## T11｜端到端验证

影响文件：无

依赖：T1~T10

工作：按 `checklist.md` 逐条运行，贴命令 + 输出作为证据；全绿后 spec 标"已完成"，文件夹挪入 `specs/_done/`。
