# 0006 checklist —— 可运行、可观测

每条跑出 pass/fail，跑完贴证据（命令 + 输出）。`<填>` 处是 spec 砍掉、落在此处的固定值；
实现须与之一致。

> 验收结论：全部 PASS。单测 124 passed（新增 24）；e2e 在 DeepSeek anthropic 端点跑通纯对话 +
> 多轮工具循环，端点回传缓存字段且滚动缓存真实命中（cache_read 跨轮递增 0→1408→1664→1792）。

## 固定值（实现须严格匹配）

- 7 个职责模块关键词：`身份`、`行为`、`工具使用`、`代码规范`、`安全边界`、`任务模式`、`输出风格`
- 双重强化规则 A 文案锚点：`优先使用专用工具`（出现在系统提示词 **且** `run_command` 工具 description）
- 双重强化规则 B 文案锚点：`编辑前先读`（出现在系统提示词 **且** `edit_file` 工具 description）
- plan 全量提醒须含：`plan 模式` **且** `会被记录` **且** `不会执行`
- plan 精简提醒须含：`plan 模式`，**且不含**整句 `会被记录`（明显短于全量）
- 环境块标签：`<env>` … `</env>`，块内含字段关键字 `cwd`、`os`、`date`、`git`
- 动态提醒标签：`<system-reminder>` … `</system-reminder>`
- 缓存字段名：`cache_read_input_tokens`、`cache_creation_input_tokens`
- 缓存断点总数上限：`≤ 4`

## T1 模块化拼接

- [x] `grep -nE "身份|行为|工具使用|代码规范|安全边界|任务模式|输出风格" coreagent/prompts.py` 命中 ≥7 行
      → 输出 `16`（≥7）。
- [x] `python -c "...build_system_prompt(['read_file'])... all(k in s ...)"` 输出 `True`
- [x] 装配后系统文本仍含工具清单：`'read_file' in b(['read_file'])` → `True`

## T7 双重强化（系统 + 工具描述）

- [x] 系统提示词含规则 A：`'优先使用专用工具' in b([])` → `True`
- [x] `run_command` 工具 description 含规则 A：`grep -c "优先使用专用工具" coreagent/tools/run_command.py` → `1`
- [x] 系统提示词含规则 B：`'编辑前先读' in b([])` → `True`
- [x] `edit_file` 工具 description 含规则 B：`grep -c "编辑前先读" coreagent/tools/edit_file.py` → `1`

## T2 环境快照

- [x] 环境块含四类字段：`all(k in build_env_block() for k in ['cwd','os','date','git'])` → `True`
- [x] 环境块带标签：`'<env>' in blk and '</env>' in blk` → `True`
      实际块：`<env>\ncwd: /root/codeAgent\nos: Linux-...\ndate: 2026-06-05\ngit: 分支 main，N 处未提交改动\n</env>`
- [x] git 子进程只调一次：`tests/test_prompts.py::test_git_subprocess_called_once`（monkeypatch 计数 == 1）PASS
- [x] 非 git 目录降级不抛错：`test_non_git_dir_degrades_without_raising` —— git 字段 == `非 git 仓库 / 不可用`，不抛异常，PASS

## T3 动态注入 + 节奏

- [x] 首轮全量：`build_reminder(plan_only=True, round_index=1)` 含 `plan 模式` 且 `会被记录` 且 `不会执行` → `True True True`
- [x] 第 2 轮起精简：`round_index=2` 含 `plan 模式`、不含整句 `会被记录`、长度 < 首轮 → `True True True`
- [x] 非 plan 不注入：`build_reminder(plan_only=False, round_index=1)` 返回 `None`（无 `plan 模式`）
- [x] 提醒带标签：全量文本含 `<system-reminder>` 与 `</system-reminder>` → `True`

## T4/T5 缓存断点落位（结构验收，单测）

- [x] Anthropic `system` 为块列表，首块（稳定）带 `cache_control`、尾块（环境）不带：
      `tests/test_providers.py::test_system_stable_block_cached_env_block_not` PASS；
      实请求落位 `test_anthropic_request_params_carry_cache_control` PASS
- [x] `messages` 最后一条已定型消息带滚动 `cache_control`：`test_messages_last_finalized_gets_rolling_cache` PASS
- [x] 全请求 `cache_control` 断点计数 ≤ 4：`test_total_cache_breakpoints_within_limit`（实测 tools+system+messages = 3）PASS
- [x] OpenAI 行为不变：`test_openai_flattens_system_no_cache_no_tools` —— 单条 system 字符串、`_count_cache_control == 0`、
      `'tools' not in captured`，PASS；既有 provider 测试仍全绿

## T5 动态提醒位置 + 不落历史

- [x] 动态提醒追加在滚动断点之后：`test_reminder_after_rolling_breakpoint_and_uncached`（reminder_idx > rolling_idx）PASS
- [x] env 不入持久化历史：e2e 多轮跑后 `history has <env>: False`
- [x] reminder 不入持久化历史：e2e 多轮跑后 `history has <system-reminder>: False`
- [x] `conversation.messages` 不含 env / reminder：`tests/test_agent.py::test_env_and_reminder_injected_not_persisted`
      —— 历史 == 2 条（user/assistant），dumped 不含 env/reminder，PASS

## T6 缓存可观测

- [x] usage 含缓存字段时记录：`test_cache_usage_exposed_on_done` —— DONE.cache_read_input_tokens==128 且日志含 `cache_read_input_tokens=128`，PASS
- [x] usage 缺字段时降级：`test_cache_usage_missing_degrades` —— DONE 字段为 None 且日志含 `端点未回传缓存字段`，不抛错，PASS

## #0004/#0005 回归（不变量）

- [x] `uv run pytest tests/test_agent.py -q` 全绿（顺序分离、取消三态、`plan_items` 产出语义未变）
- [x] plan_only 仍记计划项：`test_plan_only_intercepts_writes_runs_reads` —— `LOOP_DONE.plan` 非空，PASS
- [x] `uv run pytest -q` 全套全绿 → `124 passed in 3.86s`

## 端到端（至少一条）

- [x] DeepSeek anthropic 端点跑通纯对话：`COREAGENT_LOG_LEVEL=INFO uv run coreagent --config config.yaml`
      （pty 注入一句）→ 终端正常中文回复「CoreAgent 是运行在终端中的 AI 编程助手，能够通过读写文件、
      搜索代码和执行命令等工具，直接在真实代码库上帮助用户完成开发任务。」；HTTP/1.1 200 OK；
      stderr 出现缓存字段：`缓存命中 cache_read_input_tokens=2304，创建 cache_creation_input_tokens=0`
- [x] 多轮工具循环跑通：经 `run_agent_turn` 驱动「读 note.txt → 把 world 改成 CoreAgent → 再读确认」，
      4 轮自然完成（LOOP_DONE natural），文件实测由 `hello world` → `hello CoreAgent`；
      history.json 无 `<env>` / `<system-reminder>`；滚动缓存真实命中：cache_read 跨轮 0→1408→1664→1792。
