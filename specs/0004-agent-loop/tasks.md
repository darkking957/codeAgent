# tasks — Agent 循环

> 顺序：先做可独立验证的事件流与循环本体，再叠并发 / 取消 / plan，最后接 TUI 主流程与端到端。
> 每个任务一次会话内做完。参考定位为现状文件 + 函数 / 行号（基于当前 working tree）。

---

## T1｜事件流：循环级信号 + stop_reason 透出

**影响文件**：`coreagent/providers/base.py`、`coreagent/providers/anthropic.py`、`tests/conftest.py`、`tests/test_providers.py`
**依赖**：无
**参考定位**：
- `providers/base.py` 的 `ChunkType`（7–11 行）、`StreamChunk`（13–21 行）。
- `providers/anthropic.py` 末尾 `yield StreamChunk(ChunkType.DONE, ...)`（111 行）；`final_message = await stream.get_final_message()`（72 行）可取 `final_message.stop_reason`。
- 假事件流构造器在 `tests/conftest.py: done_chunk / tool_use_done`（46–65 行）。

**做什么**：
- `ChunkType` 新增四个循环级信号：轮开始 / 轮结束 / 循环结束 / 循环错误。
- `StreamChunk` 增字段承载循环级元信息：轮次编号、stop_reason、退出原因（自然 / 上限 / 取消）、错误类型。沿用 dataclass + 可选字段，默认值不破坏现有构造。
- `anthropic.py` 的 DONE 事件附带 `stop_reason`（读 `final_message.stop_reason`）。
- conftest 的 `done_chunk` 支持可选 `stop_reason`，便于断言。

**自测**：`test_providers.py` 加用例——带工具与不带工具时 DONE 均带 stop_reason；纯对话 DONE 形态不变。

---

## T2｜循环本体：run_agent_turn 改造为多轮 while-loop

**影响文件**：`coreagent/agent.py`、`tests/test_agent.py`
**依赖**：T1
**参考定位**：
- `agent.py: run_agent_turn`（36–127 行）当前是「第 1 轮 + 第 2 轮」硬编码两段；`MAX_MODEL_CALLS = 2`（27 行）、`_EMPTY_REPLY_PLACEHOLDER`（29 行）。
- 第 1 轮流式与解析（49–73 行）、第 2 轮（107–127 行）合并进 while 循环体。
- `stream_with_retry`（`retry.py:34`）每轮调用形态不变。

**做什么**：
- 把两段硬编码改成 `while` 循环：循环体 = 调模型 → 收集 text/blocks/tool_calls → 若无 tool_calls 写 assistant 并 `break`（自然终止）→ 否则写含 tool_use 的 assistant、执行工具、回灌 tool_result、轮次 +1。
- `MAX_MODEL_CALLS` 改为轮数上限常量（值见 checklist）；达上限：不再调模型，emit 循环结束（原因 = 上限），把已产文本作为最终答复（沿用空文本占位逻辑）。
- 每轮前 emit 轮开始（带轮次）、每轮模型流结束后 emit 轮结束（带 stop_reason）、循环退出时 emit 循环结束（带原因）。
- **不可恢复错误路径**：模型调用 / 流式在 `stream_with_retry` 退避耗尽后上抛的异常，由循环捕获并 emit 循环错误（带错误类型）后终止——**不**与单个工具失败混淆（工具失败仍由 `registry.execute` 兜底成结构化 tool_result，循环继续，见 #0003）。
- 工具执行暂沿用现有串行 for（77–104 行），并发 / 取消 / plan 在 T3–T5 叠加。

**自测**：改写 `test_agent.py: test_turn_cap_second_round_tool_ignored`（80 行）为多轮断言——连环三轮工具后自然终止；无工具调用恰一轮（`test_tools_seen_carries_api_list` 95 行仍通过）；构造「永不停」脚本验证达上限停；构造 provider 抛不可恢复异常的脚本验证 emit 循环错误（而非裸上抛、而非循环结束-上限）。

---

## T3｜工具分类执行：读类并发、写类串行、按原序回灌

**影响文件**：`coreagent/agent.py`、`tests/test_agent.py`
**依赖**：T2
**参考定位**：
- `agent.py` 逐个处理工具调用的 `for tc in tool_calls1`（77–104 行）。
- `registry.execute`（`tools/registry.py:58`）已用 `asyncio.to_thread`，可被 `asyncio.gather` 并发包裹。
- 读 / 写判据 = `tool.requires_confirmation`（`tools/base.py:46`，读 = False / 写 = True）；`registry.get_optional`（`registry.py:41`）。

**做什么**：
- 一轮内按 `requires_confirmation` 把 tool_calls 切成读类 / 写类两组（保留各自原索引）。
- 读类用 `asyncio.gather` 并发执行；写类逐个 confirm → execute 串行。
- **tool_result 按模型原调用顺序组装并回灌历史**（确定性）；**on_tool 按完成先后 emit**（实时进度，不要求有序）。两者顺序语义有意分开——别把 on_tool 也强行排成调用序。

**自测**：`test_agent.py` 加用例——一轮含两读一写：① 用带可控时延的假读工具让「调用序靠后者先完成」，断言 **tool_result 顺序 = 原调用序**、而 **on_tool result 顺序 = 完成序（与调用序不同）**，两条顺序在同一用例里同时断言；② 写类触发 confirm；③ 混合不串味。

---

## T4｜取消令牌：边界检查 + 优雅补齐 tool_result

**影响文件**：`coreagent/agent.py`、`tests/test_agent.py`
**依赖**：T2、T3
**参考定位**：
- `run_agent_turn` 签名（36–45 行）加可选取消令牌（`asyncio.Event`）。
- 写 assistant_blocks（70–71 行）与 add_tool_results（105 行）之间是悬挂 tool_use 的风险窗口。
- `conversation.add_tool_results`（`conversation.py:42`）用于补齐。

**做什么**：
- 每轮开始、每个工具执行前检查取消令牌。
- 取消时：对本轮**已发起但取消时仍未产出真实结果的 tool_use**（尚未开始执行、或已发起但被中断未完成）补齐占位 tool_result（标记已取消，文案见 checklist）；**已完成的（含正在执行、等待其跑完的廉价读类）保留真实结果，不替换**；emit 循环结束（原因 = 取消）后退出。
- 仅当本回合尚未产出任何工具 / assistant 块（末条仍纯文本 user 提问）时，**不把该 user 提问追加进历史**（视作未提交，非历史回滚）；已进工具阶段则保留并配平、不动既有消息。

**自测**：`test_agent.py` 加用例——执行中途置取消令牌：历史中 tool_use 数 == tool_result 数（配平）；`Conversation` 存盘再 load 不报错。

---

## T5｜plan-only：写类拦截、记计划项、回灌占位、循环续跑

**影响文件**：`coreagent/agent.py`、`tests/test_agent.py`
**依赖**：T2、T3
**参考定位**：
- `run_agent_turn` 写类处理分支（79–104 行）：plan 模式下写类不进 `registry.execute`。
- `run_agent_turn` 签名（36–45 行）加 plan-only 开关入参 + 计划收集出口（生成器末尾产出或回调）。

**做什么**：
- 入参带 plan-only 标志。开启时：读类照常执行；写类拦截——不执行、不 confirm，记一条计划项（工具名 + 关键参数，格式化为一行），回灌「已记录未执行」结构化结果（文案见 checklist），循环照常推进。
- 循环结束把计划列表作为结构化输出交还调用方（供 TUI 展示）。

**自测**：`test_agent.py` 加用例——plan 模式下：写类工具 `.calls == []`（未执行）、读类照跑（`.calls != []`）、**读类的 on_tool 事件正常 emit**（拦截写类没有误伤事件发送路径）、返回的计划列表含被拦截写类的名与参数。

---

## T6｜接入主流程：TUI 接循环 + /plan + 取消令牌 + 计划展示

**影响文件**：`coreagent/tui.py`
**依赖**：T2、T3、T4、T5
**参考定位**：
- `tui.py: run`（330 行）主循环；`async for chunk in run_agent_turn(...)`（375–393 行）；`_render_chunk`（266 行）；命令分派 `/clear`/`/help`（353–361 行）；`_toolbar`（163 行）；`KeyboardInterrupt` 处理（395–399 行）与 `_rollback_pending_user`（254 行）；`_CommandCompleter._CMDS`（92 行）。

**做什么**：
- `_render_chunk` 加循环级信号分支：轮开始（仅第 2 轮起显示分组标记，形如分隔行 `─── 第 2 轮 ───`，保纯对话单轮场景不变）、轮结束、循环结束（按原因显示「完成 / 已达上限 / 已取消」）、循环错误（显示错误类型）。
- 加 `/plan` 命令切换 plan-only 开关；`_toolbar` 与 `_CommandCompleter` 反映模式。
- 流式期间 Ctrl+C → set 取消令牌（经临时 SIGINT handler，参考 `asyncio.get_running_loop().add_signal_handler`），让循环优雅收尾，替代当前裸 `KeyboardInterrupt` 回滚。
- plan-only 循环结束后展示计划列表，提示确认；确认即退出 plan-only（不自动执行）。

**自测**：见 checklist 的 TUI 手动验收。

---

## T7｜端到端验证（真实模型四路径）

**影响文件**：无（手动 e2e）
**依赖**：T1–T6
**参考定位**：按 memory `project_e2e_setup` —— `config.yaml` 走 DeepSeek 的 Anthropic 端点（支持 tool_use）；dev 工具装包需 `--break-system-packages`。
**做什么**：跑 checklist 的四条 e2e（连环多工具、触发上限、中途取消配平、plan 拦截与确认退出），贴命令 + 输出。
