# checklist — Agent 循环

> 每条跑出 pass / fail，跑完贴证据（命令 + 输出）。本文件锁定 spec 砍掉的具体值。
> 离线测试统一入口：`pytest tests/test_agent.py tests/test_providers.py -q`。

## 固定值（实现须精确匹配）

- [ ] **轮数上限 = 25**：一个用户输入内模型调用封顶 25。
      `grep -nE "25" coreagent/agent.py` 命中轮数上限常量定义。
- [ ] **取消补齐 tool_result 文案精确为**「已取消执行该工具」。
      `grep -n "已取消执行该工具" coreagent/agent.py` 返回 ≥1。
      （前提：此占位仅用于取消时**尚未产出真实结果**的 tool_use——尚未开始、或已发起但被中断未完成；已完成的工具保留真实结果，不套这条文案。详见 T4。）
- [ ] **plan 拦截回灌文案精确为**「plan-only 模式：已记录该操作，未执行」。
      `grep -n "plan-only 模式：已记录该操作，未执行" coreagent/agent.py` 返回 ≥1。
- [ ] **进入 plan 提示精确为**「已进入 plan-only 模式（只规划、不执行写操作）」。
      `grep -n "已进入 plan-only 模式" coreagent/tui.py` 返回 ≥1。
- [ ] **退出 plan 提示精确为**「已退出 plan-only 模式」。
      `grep -n "已退出 plan-only 模式" coreagent/tui.py` 返回 ≥1。
- [ ] **计划列表标题精确为**「待执行计划：」。
      `grep -n "待执行计划：" coreagent/tui.py` 返回 ≥1。

## T1 事件流 / stop_reason

- [ ] `ChunkType` 含四个循环级信号（轮开始 / 轮结束 / 循环结束 / 循环错误）。
      `grep -nE "TURN_START|TURN_END|LOOP_DONE|LOOP_ERROR" coreagent/providers/base.py` 返回 4 行（或等价命名各 1）。
- [ ] anthropic DONE 透出 stop_reason：`grep -n "stop_reason" coreagent/providers/anthropic.py` 返回 ≥1。
- [ ] `test_providers.py` 新增用例断言带工具 / 不带工具 DONE 均带 stop_reason → pass。
- [ ] 纯对话 DONE 形态回归：`pytest tests/test_providers.py -q` 全绿。

## T2 循环本体与终止

- [ ] 多轮连环：脚本「读 a → 读 b → 文本」三段，模型被调 3 次、两个读各执行一次、末条 assistant 为最终文本。
      新用例 `test_loop_multi_round_until_no_tool` → pass。
- [ ] 自然终止：无工具调用时循环恰一轮（`prov.calls == 1`）。
      `test_tools_seen_carries_api_list`（沿用）→ pass。
- [ ] 达上限停：构造「每轮都出工具调用」的永动脚本，`prov.calls == 25` 后停、emit 循环结束（原因 = 上限）。
      新用例 `test_loop_stops_at_round_cap` → pass。
- [ ] **不可恢复错误 → 循环错误**：构造 provider 在重试耗尽后抛不可恢复异常的脚本，断言循环 emit **循环错误信号（带错误类型）并终止**，而非裸上抛、也非循环结束(上限)；同时确认单个工具失败（execute 返回 fail）**不会**触发循环错误、循环继续。
      新用例 `test_loop_emits_loop_error_on_unrecoverable` + `test_tool_failure_is_structured_not_loop_error` → pass。
- [ ] emit 了轮开始 / 轮结束 / 循环结束信号：drain 出的 chunk 序列含三类循环级信号。

## T3 工具分类与并发顺序

- [ ] 一轮含「读 a、读 b、写 c」：两读并发执行，回灌的 tool_result 顺序 == 模型原调用序（a,b,c）。
      新用例 `test_reads_concurrent_results_in_call_order` 断言 `[r.tool_use_id ...]` 顺序 → pass。
- [ ] **on_tool 走完成序、tool_result 走调用序（二者有意不同）**：用带可控时延的假读工具，让调用序靠后的读先跑完；同一用例同时断言 ① tool_result 顺序 == 调用序，② on_tool result 事件顺序 == 完成序（≠ 调用序）。
      新用例 `test_on_tool_completion_order_vs_result_call_order` → pass。（防止实现者把两者都排成有序、或都放成乱序。）
- [ ] 写类仍逐个触发 confirm；读类不触发（沿用 `test_confirm_called_for_write_not_read` 思路扩到同轮混合）→ pass。
- [ ] 读类确实并发（非串行）：用带可控阻塞的假读工具，**断言两读 execute 进入时间重叠**——即第二个读的开始早于第一个读的结束（不用「总耗时」类时长比较，避免 CI 调度抖动偶发 fail）。

## T4 取消配平

- [ ] 中途取消：执行阶段置取消令牌后，会话历史里 tool_use 块数 == tool_result 块数（配平、无悬挂）。
      新用例 `test_cancel_balances_tool_use_and_result` → pass。
- [ ] 取消后历史可重放：`Conversation.save` 到临时文件再 `load`，不抛异常、消息数一致。
- [ ] 取消时 emit 循环结束（原因 = 取消）。
- [ ] 占位前提分两种均覆盖：取消时**尚未开始**执行的 tool_use、与**已发起被中断未完成**的 tool_use，都补「已取消执行该工具」；正在执行而等其跑完的廉价读类保留真实结果（不套占位）。
- [ ] 取消发生在首字节前（末条仍纯文本 user）→ **不把该 user 提问追加进历史**（视作未提交，非历史回滚）；已进工具阶段 → 保留并配平、不动既有消息。两分支各一断言 → pass。

## T5 plan-only 拦截

- [ ] plan 模式下写类不执行：假写工具 `.calls == []`；读类照常 `.calls != []`。
      新用例 `test_plan_only_intercepts_writes_runs_reads` → pass。
- [ ] **plan 模式下读类 on_tool 事件正常 emit**：断言读类的 on_tool 仍被调用（拦截写类没有误伤事件发送路径，避免静默漏发）。
- [ ] 被拦截写类回灌「plan-only 模式：已记录该操作，未执行」结构化结果。
- [ ] 循环结束返回的计划列表含被拦截写类的工具名与关键参数。

## T6 TUI 接入（手动）

- [ ] `/plan` 可在 completer 中补全（输入 `/pl` 提示 `/plan`）。
- [ ] 输入 `/plan` 终端输出含「已进入 plan-only 模式」；底栏出现 plan 标识；再 `/plan` 输出含「已退出 plan-only 模式」。
- [ ] 多轮循环时第 2 轮起出现轮分组标记（形如分隔行 `─── 第 2 轮 ───`）；单轮纯对话无任何循环标记（与 #0003 视觉一致）。
- [ ] 流式中按 Ctrl+C：输出「已取消」、不抛裸栈、下一条输入可正常继续（历史未损坏）。

## 回归（纯对话 / 单轮不变）

- [ ] `pytest -q` 全套绿（含 #0001/#0002/#0003 既有用例）。
- [ ] 纯对话路径产出与 #0003 一致：不传工具时一次模型调用、DONE 后换行，无新增可见信号。

## 端到端（真实模型，四路径）

> 按 memory `project_e2e_setup`：`config.yaml` 走 DeepSeek Anthropic 端点；`python -m coreagent --config config.yaml` 启动。

- [ ] **连环多工具**：让助手「读 X 并按内容改 Y 再验证」，观察一个输入内多轮工具连环、最终自然作答。贴终端片段。
- [ ] **触发上限**：构造需 >25 步的任务（或临时调小常量复现），观察到「已达上限」收尾、进程不挂。贴片段。
- [ ] **中途取消**：连环执行中按 Ctrl+C，观察「已取消」收尾；`/exit` 重进后历史可继续对话（不报 tool_use 悬挂错误）。贴片段。
- [ ] **plan 拦截与退出**：`/plan` 后让助手做含写操作的任务，观察写类被记入「待执行计划：」列表而未落盘；确认退出 plan-only 后再执行才真正写。贴片段 + `git status` / 文件 mtime 佐证 plan 阶段未改盘。
