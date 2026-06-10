# checklist: 工程化加固

完成标准：每条贴命令 + 输出，不可只打勾。所有项在无 API key / 无网络下可跑。

固定值（spec 砍出的具体值落这里）：

- 非思考模式默认输出上限 = **8192**（纠正原 8096 笔误）。
- 思考模式默认输出上限 = **16000**。
- 历史损坏备份文件名 = 原文件名加 **`.bak`** 后缀。
- 日志级别环境变量 = **`COREAGENT_LOG_LEVEL`**，默认 **`WARNING`**。
- 重试默认次数 = **3**，退避 = **1 / 2 / 4** 秒。
- 可重试：连接 / 超时 / 限流（429）/ 服务端（≥500）；不可重试：其余（401 / 400 / 403 / 404 / 422 等）。

---

## 工具链

> 环境为 externally-managed，用 `python3 -m venv --system-site-packages .venv` 后 `.venv/bin/pip install -e ".[dev]"`（命令等价）。

- [x] `pip install -e ".[dev]"` 成功，`pydantic`、`pytest`、`ruff`、`mypy` 可用。
  证据：`Successfully installed ... pytest-9.0.3 pytest-asyncio-1.4.0 ruff-0.15.15 mypy-2.1.0 coreagent-0.1.0`；`pydantic 2.13.4 / ruff 0.15.15 / pytest 9.0.3 / mypy 2.1.0`。
- [x] `ruff check coreagent` 退出码 0（无 lint 报错）。
  证据：`All checks passed!` `ruff exit=0`（先修复了 #0001 遗留的 `base.py` 未用 `field` 导入）。
- [x] `pytest -q` 全部通过，且过程**无网络请求**（用例使用假 provider）。
  证据：`28 passed in 1.47s`；全部用例由 `ScriptedProvider` / 假 SSE / SimpleNamespace 驱动，无真实 client 调用。
- [x] `grep -n "pydantic" pyproject.toml` 命中运行时依赖；`grep -n "pytest-asyncio" pyproject.toml` 命中 dev 依赖。
  证据：`12: "pydantic>=2"`（dependencies）；`18: "pytest-asyncio>=0.23"`（optional-dependencies.dev）。
- [x] `mypy coreagent` 一键可跑且零错误。
  证据：`Success: no issues found in 12 source files`，exit 0。
  （修法：`BaseProvider.stream_chat` 用「返回 `AsyncIterator` 的抽象方法」惯用签名替代 `async def`，消除 override 摩擦——运行时契约不变；OpenAI 调用处在 SDK 边界 `cast` 消息为 `ChatCompletionMessageParam`，使 `stream=True` 重载解析为 `AsyncStream`。）

---

## 配置校验

- [x] `python3 -c "...load_config('config.yaml')...print(c.protocol, c.model)"` 正常输出。
  证据：`anthropic deepseek-v4-flash`。
- [x] `${ENV}` 解析：`api_key: ${TEST_VAR}` 时，`TEST_VAR=abc` 解析为 `abc`。
  证据：`TEST_VAR=abc ... → api_key = abc`（亦见 `test_env_resolution`）。
- [x] protocol 非法 → `ConfigError`，含 `protocol` / `anthropic`/`openai` / 当前值。
  证据：`配置校验失败：配置字段 protocol 取值非法：当前为 'grpc'，期望 'anthropic' 或 'openai'`。
- [x] `api_key` 解析后为空 → `ConfigError`，含 `api_key` 且提示 `${ENV_VAR}`。
  证据：`...配置字段 api_key 不能为空（可用 ${ENV_VAR} 从环境变量读取，例如 api_key: ${ANTHROPIC_API_KEY}）`。
- [x] `model` 为空 → `ConfigError`，含 `model`。
  证据：`配置校验失败：配置字段 model 不能为空`。
- [x] `thinking.enabled: true` 且 `budget_tokens <= 0` → `ConfigError`，含 `budget_tokens`。
  证据：`...配置字段 thinking.budget_tokens 必须为正整数：当前为 0`。
- [x] 缺省时：非思考上限默认 8192、思考上限默认 16000。
  证据：`max_tokens = 8192 | thinking_max_tokens = 16000`。
- [x] 指定不存在配置：`python3 -m coreagent --config nonexistent.yaml` 输出含"未找到"，退出码非 0。
  证据：stdout `未找到配置文件：nonexistent.yaml`，`exit=1`。

---

## 持久化（原子 + 容错）

- [x] roundtrip：消息一致；assistant 带 thinking 写成内容块列表。
  证据：`test_roundtrip PASSED`（断言 content 为 list、首块 thinking、次块 text）。
- [x] 原子写入：`save` 后目录内无残留临时文件；`grep -n "replace"` 命中原子替换。
  证据：`test_atomic_no_temp_residue PASSED`（目录仅 `history.json`）；`conversation.py:56: os.replace(tmp, path)`。
- [x] 损坏恢复：非法 JSON `load` 不抛异常、返回空历史，生成 `<原名>.bak`，日志告警。
  证据：`test_corrupt_recovery PASSED` + `test_corrupt_structure_recovery PASSED`（生成 `history.json.bak`，caplog 含"损坏"）。
- [x] 文件缺失：`load` 返回空 `Conversation`。
  证据：`test_missing_file_returns_empty PASSED`。
- [x] `/clear` 后 `save`，文件内容为 `[]`。
  证据：`test_clear_then_save_is_empty_list PASSED`；真实 CLI `/clear` 打印 `✓ history cleared`。

---

## 重试（分类 + 守卫）

- [x] `grep -n "max_retries"` 可见默认值 `3`。
  证据：`retry.py:38: max_retries: int = 3,`。
- [x] `grep -n "_is_retryable"` 命中分类函数。
  证据：`retry.py:18: def _is_retryable(exc: BaseException) -> bool:`。
- [x] 可重试错误（连接/429/503）：重试 3 次，退避 `[1, 2, 4]`，`on_retry` 依次 1/2/3。
  证据：`test_retryable_backoff[exc0/1/2] PASSED`（`waits==[1,2,4]`，`retries==[(1,1),(2,2),(3,4)]`，`calls==4`）。
- [x] 不可重试错误（status_code=401）：0 次重试，立即上抛。
  证据：`test_non_retryable_immediate PASSED`（`calls==1`，无 sleep、无 on_retry）。
- [x] 已输出守卫：先 yield 一个 chunk 再抛错 → 不重试，异常直接上抛。
  证据：`test_already_yielded_no_retry PASSED`（已得 `partial`，`calls==1`）。
- [x] 表现层解耦：`grep -n "print(" coreagent/retry.py` 无命中。
  证据：`grep exit=1`（无命中），改用 `on_retry` 回调 + `logging`。

---

## providers

- [x] `grep -rn "8096" coreagent/` 无命中（笔误已清除）。
  证据：`grep exit=1`（无命中）；默认值改为 8192。
- [x] Anthropic 输出上限取自 config（非硬编码）。
  证据：`anthropic.py:22-25` `max_tokens = self.config.thinking_max_tokens if ... else self.config.max_tokens`。
- [x] `create_provider`：`anthropic`→`AnthropicProvider`、`openai`→`OpenAIProvider`、未知→`ValueError` 含"不支持"。
  证据：`test_create_provider_anthropic/openai/unknown PASSED`（unknown 抛 `不支持的 protocol：'grpc'`）。
- [x] 假 SSE 驱动 `AnthropicProvider.stream_chat`：顺序 THINKING…→TEXT…→DONE。
  证据：`test_anthropic_stream_order PASSED`（首块 THINKING、末块 DONE、所有 THINKING 在 TEXT 前、DONE 携带 thinking+text 块）。

---

## 日志

- [x] 默认（未设 `COREAGENT_LOG_LEVEL`）：不产生 INFO/DEBUG 噪声到对话区。
  证据：默认 `coreagent` logger effective level = `WARNING`，`log.debug/info` 无输出；缺失配置时 stderr 为空，仅 stdout 友好信息。
- [x] `COREAGENT_LOG_LEVEL=DEBUG ... --config nonexistent.yaml` 能看到更详细日志。
  证据：stderr 出现 `... DEBUG coreagent.main: 配置文件缺失` + Traceback；stdout 仍仅 `未找到配置文件：...`，`exit=1`。日志写 stderr，与对话区（stdout）解耦。

---

## 端到端

- [x] 构造 `TUI(假provider, config, conversation)`，`_print_banner()` / `_print_help()` 不抛异常。
  证据：`test_banner_and_help_no_raise PASSED`。
- [x] 假 provider 触发可重试错误跑完整流式路径：界面出现"第 N 次重试…"，最终错误以 `✗` 展示，末条 user 消息被回滚。
  证据：`test_retry_path_rollback PASSED`（stdout 含"重试"与"✗"，`conv.messages == []`，`provider.calls == 4`）。
- [x] 完整流程：启动 → `/help` → `/clear` → `/exit`，全程无异常。
  证据：`test_full_flow_help_clear_exit PASSED`；真实 pty 驱动 CLI：横幅 → commands 表 → `✓ history cleared` → `goodbye` 后进程正常退出。
