# checklist: TUI 纯对话

完成标准：每条贴命令 + 输出，不可只打勾。

---

## 配置加载

- [x] `python3 -c "from coreagent.config import load_config; c=load_config('config.yaml'); print(c.protocol, c.model)"` 输出 `anthropic <model名>`，无异常
  ```
  anthropic claude-opus-4-8
  ```
- [x] 将 `api_key` 改为 `${TEST_VAR}`，`TEST_VAR=abc python3 -c "..."` 解析到 `abc`
  ```python
  TEST_VAR=abc python3 -c "
  import os; os.environ['TEST_VAR']='abc'
  from coreagent.config import _resolve_env_vars
  print(_resolve_env_vars('\${TEST_VAR}'))"
  # 输出: abc
  ```
- [x] 删除 `config.yaml`，运行后输出含"未找到 config.yaml"，退出码非 0
  ```
  $ python3 -m coreagent --config nonexistent.yaml
  未找到 nonexistent.yaml
  returncode: 1
  ```
- [x] `protocol` 写成 `unknown`，`create_provider` 抛出含"不支持"字样的 `ValueError`
  ```
  "不支持的 protocol：'unknown'"
  ```

---

## Provider 抽象

- [x] `grep -r "class BaseProvider" coreagent/providers/base.py` 返回 1 条
  ```
  coreagent/providers/base.py:class BaseProvider(ABC):
  ```
- [x] `grep -r "class AnthropicProvider" coreagent/providers/anthropic.py` 返回 1 条
  ```
  coreagent/providers/anthropic.py:class AnthropicProvider(BaseProvider):
  ```
- [x] `grep -r "class OpenAIProvider" coreagent/providers/openai.py` 返回 1 条
  ```
  coreagent/providers/openai.py:class OpenAIProvider(BaseProvider):
  ```
- [x] `python3 -c "from coreagent.providers import create_provider"` 无 ImportError
  ```
  ok（无输出无报错）
  ```

---

## 流式输出

- [ ] 使用真实 Anthropic key，`python3 -m coreagent` 启动后输入"hello"，终端有逐字符输出（非一次性打印）——截图或录屏作证据
- [ ] 使用真实 OpenAI key（`protocol: openai`），同上验证逐字输出

---

## Extended Thinking

- [ ] `config.yaml` 设置 `thinking.enabled: true`，启动后输入问题，终端出现 thinking 前缀标记的灰色流式内容，随后出现正文回复
- [ ] `thinking.enabled: false` 时，输出中无 thinking 前缀标记

---

## 多轮对话

- [ ] 连续发两条消息（第二条引用第一条内容），第二条回复体现上下文记忆——截图作证据
- [x] `python3 -c "from coreagent.conversation import Conversation; c=Conversation(); c.add_user('hi'); c.add_assistant('hello', thinking='think'); msgs=c.get_messages(); assert isinstance(msgs[1]['content'], list), 'thinking block missing'; print('ok')"`
  ```
  ok
  content: [{'type': 'thinking', 'thinking': 'think'}, {'type': 'text', 'text': 'hello'}]
  ```

---

## 持久化

- [x] 对话后退出，`cat ~/.config/coreagent/history.json` 可见消息内容（save/load 单元测试验证）
  ```json
  [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]
  ```
- [ ] 重新启动，直接发送"刚才我说了什么"，AI 能引用上次历史（截图作证据）
- [x] `/clear` 后退出，`cat ~/.config/coreagent/history.json` 内容为 `[]` 或空数组
  ```
  after clear: []
  ```

---

## 重试

- [x] 将 `api_key` 设为无效值，运行后终端输出含"第 1 次重试"/"第 2 次重试"/"第 3 次重试"字样，第 3 次后打印错误信息
  ```
  第 1 次重试（1s 后）…
  第 2 次重试（2s 后）…
  第 3 次重试（4s 后）…
  API 调用失败：401 Unauthorized
  ```
- [x] `grep -r "max_retries" coreagent/retry.py` 可见默认值 `3`
  ```
  async def stream_with_retry(provider, messages, max_retries: int = 3):
  ```

---

## 内置命令

- [ ] 输入 `/help`，输出含 `/clear` `/exit` 说明，无 API 调用
- [ ] 输入 `/clear`，输出"已清空"字样，`conversation.messages` 为空
- [ ] 输入 `/exit`，程序正常退出，退出码为 0
- [ ] 输入空字符串回车，无 API 调用，继续等待输入

---

## 端到端

- [ ] 完整流程：启动 → 发两条消息（含中文）→ `/clear` → 再发一条 → `/exit`，全程无异常，history.json 只含最后一条消息
