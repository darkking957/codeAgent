# checklist: 工具系统（#0003）

状态：全绿（2026-06-04）。离线项经 `pytest -q`（56 passed）/ `ruff` / `mypy` 验证；
真实模型端到端经配置的 Anthropic 协议端点（DeepSeek `deepseek-v4-flash`）实跑通过。
证据见各条与文末「验收证据」。

完成标准：每条贴命令 + 输出，不可只打勾。除「端到端 / 模型对接」需真实 Anthropic key 的项外，其余在无网络下可跑。

固定值（spec 砍出的具体值落这里，实现须与此一致）：

- 六个工具名 = **`read_file` / `write_file` / `edit_file` / `run_command` / `glob` / `grep`**。
- 需确认工具 = **`write_file` / `edit_file` / `run_command`**；免确认 = **`read_file` / `glob` / `grep`**。
- `run_command` 默认超时 = **30** 秒；超时错误文案精确为 **「命令执行超时（超过 30 秒）」**。
- `edit_file` 0 匹配文案精确为 **「edit_file 失败：未找到要替换的文本（old_string 在文件中 0 处匹配）」**。
- `edit_file` 多匹配文案精确为 **「edit_file 失败：old_string 在文件中匹配到 N 处，请提供更长、唯一的上下文」**（N 为实际匹配数）。
- `read_file` 文件不存在文案精确为 **「read_file 失败：文件不存在：<path>」**。
- ripgrep 未打包平台文案精确为 **「未打包当前平台（<plat>-<arch>）的 ripgrep 二进制」**。
- 用户拒绝执行回灌文案精确为 **「用户拒绝执行该工具」**。
- 一个用户输入内模型调用次数上限 = **2**。
- ripgrep 二进制本步只落地 **linux-x64**；调用恒为 `subprocess.run([RG_PATH, ...], shell=False)`。

---

## 工具接口与注册

- [x] `grep -rn "class Tool" coreagent/tools/base.py` 命中 Tool 抽象；接口含名称 / 描述 / 参数 Schema / 执行方法 / 需确认标记。
- [x] 注册中心可登记并按名查找：注册六工具后，按名取 `read_file` 得到对应实例、取未知名报清楚错误。
- [x] 转 API 工具清单：输出为含 `name` / `description` / `input_schema`（或等价）的列表，长度 = 6。
- [x] 统一执行入口对异常做结构化包装：令某工具执行内部抛错，返回失败结果而非上抛崩溃。
- [x] 需确认标记正确：`write_file` / `edit_file` / `run_command` 为需确认，`read_file` / `glob` / `grep` 为免确认。

## 六个工具行为

- [x] `read_file`：读存在文件返回其内容；读不存在文件返回失败结果，文案 = 「read_file 失败：文件不存在：<path>」。
- [x] `write_file`：写入并自动建父目录；写后文件内容与入参一致。
- [x] `edit_file` 唯一匹配：原文恰一处匹配 → 替换成功，文件相应改变。
- [x] `edit_file` 0 匹配：返回失败，文案 = 「edit_file 失败：未找到要替换的文本（old_string 在文件中 0 处匹配）」。
- [x] `edit_file` 多匹配：原文出现 ≥2 处 → 返回失败，文案含「匹配到 N 处」与「更长、唯一的上下文」，不修改文件。
- [x] `run_command`：`echo hello` 类命令 → 成功结果含 stdout、returncode=0。
- [x] `run_command` 超时：跑 `sleep 60`（默认超时 30s）→ 失败结果，文案 = 「命令执行超时（超过 30 秒）」，进程不挂死。
- [x] `glob`：临时目录树 + `.gitignore`（忽略某文件）→ 匹配结果**不含**被忽略项、且不含 `.git` 下路径。
- [x] `grep`：临时目录树中含某模式的行被命中，输出含 `file:line`；`grep -rn "subprocess.run(\[" coreagent/tools/grep_tool.py` 命中、且 `grep -rn "shell=True" coreagent/tools/` 无命中。
- [x] ripgrep 解析：`grep -rn "platform.machine" coreagent/tools/ripgrep.py` 命中；linux-x64 解析到已落地二进制（`ls coreagent/bin/` 见 linux-x64 rg），二进制可执行（`<RG_PATH> --version` 有版本输出）。
- [x] ripgrep 未打包平台：模拟非 linux-x64 → 抛文案「未打包当前平台（<plat>-<arch>）的 ripgrep 二进制」。

## Provider 工具调用解析

- [x] `grep -n "tools" coreagent/providers/base.py` 命中：`stream_chat` 签名含工具入参。
- [x] 假事件流（仿 `tests/test_providers.py: _FakeStream`）发出**分片的** tool_use 参数 JSON 碎片 → provider 拼接后得到完整工具调用（断言组装出的工具名与参数 dict 正确）。
- [x] 完成事件携带：assistant 内容块含 tool_use 块；并提供解析后的工具调用列表供分派。
- [x] 不传工具时：THINKING / TEXT / DONE 产出顺序与 #0002 一致（既有 `test_anthropic_stream_order` 仍通过）。

## 会话工具消息块

- [x] 追加含 tool_use 的 assistant 消息与含 tool_result 的 user 消息后，`save` → `load` 往返消息结构一致、块不丢失。
- [x] 纯对话消息（user 文本 / assistant 文本 / 带 thinking 块）格式与既有 `test_roundtrip` 行为不变。

## 单轮编排与回合边界

- [x] 假 provider 首轮返回一个工具调用、二轮返回纯文本：编排执行工具一次 → 回灌 tool_result → 取到二轮文本作答 → 停；`provider.calls == 2`。
- [x] 回合上限：假 provider **两轮都**返回工具调用 → 仅第一轮工具被执行，第二轮工具调用被忽略不执行；模型调用次数 = 2。
- [x] 工具结果回灌历史：执行后会话历史出现 tool_use（assistant）与 tool_result（user）两块。

## 安全确认

- [x] 改写 / 命令工具触发确认：编排对 `write_file` / `edit_file` / `run_command` 调用 confirm 回调；读取类不调用。
- [x] 用户拒绝：confirm 回调返回「拒绝」→ 工具不执行、回灌结果文案 = 「用户拒绝执行该工具」，模型据此继续作答（不崩）。
- [x] `grep -rn "subprocess" coreagent/tools/run_command.py` 命中；确认其超时参数对应默认 30 秒。

## 系统提示词

- [x] `grep -n "工具" coreagent/prompts.py`（或等价英文）命中：提示词提及身份（终端编程助手）、可用工具、需读 / 改文件时调用工具而非臆测。
- [x] main 装配时把系统提示词接入 provider 调用（`grep -rn "prompts" coreagent/main.py` 或 `coreagent/agent.py` 命中其引用）。

## 不回归（纯对话）

- [x] `pytest -q` 全部通过（含新增工具 / 编排 / provider 用例，无网络）。
- [x] `ruff check coreagent` 退出码 0；`mypy coreagent` 零错误。
- [x] 纯对话端到端：启动 → 普通提问（模型不调工具）→ 流式作答 → `/help` → `/clear` → `/exit` 全程无异常（沿用 #0002 路径）。

## 端到端（真实模型，需 Anthropic key）

- [x] 读路径：提示「读取 <某文件> 并总结其内容」→ 模型发起 `read_file`（免确认直接执行）→ 结果回灌 → 模型据内容作答。贴终端输出（工具调用行 + 最终答复）。
- [x] 改写路径：提示「在 <某文件> 写入一行 X」→ 模型发起 `write_file` → TUI 弹确认 → 同意后执行 → 模型确认完成；另跑一次**拒绝**确认，验证回灌「用户拒绝执行该工具」后模型据此回应。贴输出。
- [x] grep 路径：提示「在代码库里搜 <某标识符>」→ 模型发起 `grep` → 返回 `file:line` 命中 → 模型据此作答。贴输出。

---

## 验收证据（命令 + 输出摘录，2026-06-04）

### 离线：测试 / 静态检查

```
$ python3 -m pytest -q
........................................................   [100%]   56 passed
$ python3 -m ruff check coreagent tests
All checks passed!
$ python3 -m mypy coreagent
Success: no issues found in 24 source files
```

新增测试文件与覆盖（`pytest -v` 名称即验收映射）：
- `tests/test_tools.py`：接口/注册/六工具行为/ripgrep 解析（20 项）——含 read 缺失文案、
  edit 0/多匹配精确文案、run echo+超时模板、glob 排除 gitignore/.git、glob 绝对 pattern、
  grep file:line、ripgrep linux-x64 解析 + 未打包平台文案。
- `tests/test_agent.py`：单轮编排/回合上限（calls==2）/确认触发与拒绝回灌（5 项）。
- `tests/test_providers.py::test_anthropic_assembles_streamed_tool_use`：分片 JSON 碎片拼接。
- `tests/test_conversation.py::test_tool_blocks_roundtrip`：tool_use/tool_result 往返不丢块。

### 离线：关键 grep / 二进制证据

```
$ grep -rn "class Tool" coreagent/tools/base.py
coreagent/tools/base.py:34:class Tool(ABC):
$ grep -rn "subprocess.run(\[" coreagent/tools/grep_tool.py
coreagent/tools/grep_tool.py:45:            proc = subprocess.run([
$ grep -rn "shell=True" coreagent/tools/   # → 无命中
$ grep -rn "platform.machine" coreagent/tools/ripgrep.py
coreagent/tools/ripgrep.py:27:    machine = platform.machine().lower()
$ ls coreagent/bin/linux-x64/      → rg (5.7MB, 可执行)
$ <RG_PATH> --version              → ripgrep 15.0.0
# 未打包平台（模拟 darwin-arm64）→ RuntimeError: 未打包当前平台（darwin-arm64）的 ripgrep 二进制
```

### 真实模型端到端（配置端点：DeepSeek `deepseek-v4-flash`，Anthropic 协议）

读路径：
```
[CALL]   read_file({'path': '…/notes.txt'})
[RESULT] success=True '项目代号：北极星。\n负责人：李雷。\n上线日期：2026-07-01。\n'
回复：该文件记录了项目代号「北极星」、由李雷负责、计划 2026-07-01 上线的项目信息。
历史 roles：user(str) → assistant(list:tool_use) → user(list:tool_result) → assistant(str)
```

grep 路径：
```
[CALL]   grep({'pattern': 'build_registry'})
[RESULT] success=True './coreagent/main.py:12:from coreagent.tools import build_registry …'
回复：列出 build_registry 出现的文件与行号。
```

改写路径（同意）：
```
[CALL]   write_file({'path': '…/poem.txt', 'content': '你好世界'})
[RESULT] success=True '已写入 …/poem.txt（4 字符）'
FILE EXISTS: True | CONTENT: '你好世界'
```

改写路径（拒绝）：
```
[CALL]     write_file({'path': '…/should_not_exist.txt', 'content': '删库跑路'})
[REJECTED] write_file
回灌 tool_result.content = 「用户拒绝执行该工具」(is_error=True)
回复：好的，看来直接写入被拒绝了……（模型据此调整，未崩）
FILE EXISTS: False
```

纯对话不回归（真实 TUI run：提问 → /help → /clear → /exit）：
```
◆ deepseek-v4-flash → ◈ thinking → 「1+1 等于 2。」
commands 表正常打印 → ✓ history cleared → goodbye；全程无异常，history 清零。
```

### 实现期发现并修复

- glob 工具原用 `pathlib.Path.glob`，遇模型传入的**绝对路径 pattern** 抛
  `NotImplementedError: Non-relative patterns are unsupported`（registry 已结构化兜底未崩，
  但工具本身应支持）。改用标准库 `glob.glob(root_dir=…, recursive=True, include_hidden=True)`，
  并加 `tests/test_tools.py::test_glob_absolute_pattern` 回归锁定。
