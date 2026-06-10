# #0005 富交互执行确认 —— 验收清单

> 每条跑出 pass/fail，跑完贴证据（命令 + 输出）。固定值（标签 / 选项 / 提示文案 / 阈值 / 周期）锁定在本文件。
>
> 状态：✅ 全绿（`uv run pytest tests/` → **100 passed**；含新增 `tests/test_confirm_ui.py` 25 项 + `tests/test_tui.py` 接入 4 项；e2e 见 T6）。

## 固定值（spec 砍掉的具体值，锁定于此）

| 项 | 值 |
|---|---|
| 区域标签文案 | `run_command`→「命令」，`write_file`→「写文件」，`edit_file`→「编辑」 |
| 选项 1 文案 | `同意执行` |
| 选项 2 文案 | `同意，本会话内不再询问 <工具名>` |
| 选项 3 文案 | `拒绝` |
| 当前项指示符 | `›`（左侧）；未选中项显示序号 `1` / `2` / `3` |
| 快捷键提示条 | 含 `↑↓`、`↵`、`esc` 三组键名 |
| 光标闪烁周期 | 0.6 秒（定时刷新间隔） |
| 预览体最大行数 | 20 行；超出截断并显示 `… +N 行` |
| 预览单行最大宽 | 100 字符；超出截断加 `…` |
| 非 TTY 降级文案 | `执行 <name>? [y/N] `（默认拒绝） |
| 拒绝回灌文案 | 沿用 #0004：`用户拒绝执行该工具`（不变） |

> 固定值落码：`coreagent/confirm_ui.py` —— `LABELS`/`INDICATOR`/`CURSOR_BLINK_INTERVAL=0.6`/`MAX_PREVIEW_LINES=20`/`MAX_LINE_WIDTH=100`/`NON_TTY_PROMPT="执行 {name}? [y/N] "`；拒绝文案沿用 `coreagent/agent.py: REJECTED_MESSAGE`（未改）。

## T1 内容预览构建

- [x] 传入 `run_command` 调用 → 标签文案精确为「命令」，预览体含命令原文。
  - `tests/test_confirm_ui.py::test_command_preview_label_and_body`：`label=="命令"`、`lexer=="bash"`、预览体含 `ls -la /tmp`。
- [x] 传入 `edit_file` 调用 → 标签「编辑」，预览体把 `old_string` 行标记为删（`-`/红）、`new_string` 行标记为增（`+`/绿）。
  - `test_edit_preview_diff_del_and_add`：`label=="编辑"`，`{ln.kind}` 含 `del`+`add`，del 文本含 `x = 1`、add 文本含 `x = 2`。渲染见 T6/T2 截图（`- return a-b` 红 / `+ return a+b` 绿）。
- [x] 传入 `write_file` 调用 → 标签「写文件」，预览体含 `path` 与 `content` 首段。
  - `test_write_preview_label_path_content`：`label=="写文件"`、`header=="pkg/mod.py"`、`lexer=="python"`，整体渲染同含 `pkg/mod.py` 与 `print('hi')`。
- [x] 预览体超 20 行 → 截断且尾部出现 `… +N 行`；某行超 100 字符 → 该行以 `…` 截断。
  - `test_preview_truncation_lines_and_width`：30 行 → `len(lines)==20`、`extra_lines==10`、渲染含 `… +10 行`；250 字符行 → `endswith("…")` 且 `len<=100`。
- [x] 缺 `command`/`content` 等字段或异常输入 → 返回兜底预览、不抛裸异常（构造空 dict 断言）。
  - `test_preview_missing_fields_fallback_no_raise`：`build_preview({})` 有兜底行；缺 input / input 非 dict / 缺 old_new 均返回正确标签、不抛异常。

## T2 六组件渲染

- [x] 渲染输出恰含三个选项，文案精确等于上表三条（含工具名插值）。
  - `test_option_labels_exact`（精确等于三条，含 `edit_file` 插值）+ `test_render_contains_three_options_exact`。
- [x] `selected_index=k` → 第 k 项左侧为 `›` 且高亮，其余项显示序号。
  - `test_selected_indicator_and_numbers`：`rows[1]` 含 `›` 且文案 span 为 `SELECTED_TEXT_STYLE`；`rows[0]`/`rows[2]` 以 `1`/`3` 起、文案 span 为 `UNSELECTED_TEXT_STYLE`。
- [x] 输出含一条细分隔线，位于「展示区（预览）」与「操作区（问句+选项）」之间。
  - `test_thin_rule_between_display_and_action`：存在一行 `set(line)=={"─"}`（纯 ─，区别于 Panel 含 ╭╮╰╯ 角的边框）；顺序见 `render_menu`（标签→框→描述→Rule→问句→选项→提示条）。
- [x] 快捷键提示条同时含 `↑↓`、`↵`、`esc`，且为低对比度样式类。
  - `test_hint_bar_keys_and_low_contrast`：`plain` 同含三组键名；所有 span 样式含 `dim`（`HINT_STYLE="dim"` / `HINT_KEY_STYLE="dim reverse"`）。
- [x] `cursor_on=True` 问句尾部含光标字符；`cursor_on=False` 不含（同一函数两次调用 diff 仅在光标位）。
  - `test_cursor_blink_diff_only_at_cursor`：`▌` 仅在 `cursor_on=True` 出现；`on.replace("▌"," ")==off`（仅差光标位）。
- [x] 命令预览的高亮 lexer 为 shell/bash；编辑预览出现增删两种着色。
  - 命令 lexer：`test_command_preview_label_and_body`（`lexer=="bash"`）。增删着色：`test_edit_render_has_add_and_del_coloring`（diff span 样式含 `red`+`green`）；ANSI 实测含 `;31`（红）与 `;32`（绿）。

## T3 内联交互应用

- [x] ↑/↓ 改变选中项并在首尾回绕；数字键 `1`/`2`/`3` 直接选中对应项。
  - `test_down_wraps_around`（↓×3→项1）、`test_up_wraps_around`（项1 ↑→项3）、`test_number_keys_direct_select`（`2`→项2、`3`→项3、`2`后 ↑→项1）。
- [x] 回车在选中项 1/2/3 时分别返回「同意」/「同意且记住」/「拒绝」三态。
  - `test_enter_returns_approve_on_default`（→APPROVE）、`test_down_then_enter_returns_approve_always`（→APPROVE_ALWAYS）、`test_two_downs_then_enter_returns_reject`（→REJECT）。
- [x] esc 返回拒绝；Ctrl+C 返回拒绝且不卡死（与流式期 SIGINT→cancel 共存，可在确认后由循环判定取消）。
  - `test_escape_returns_reject`、`test_ctrl_c_returns_reject_no_hang`（`asyncio.wait_for(timeout=5)` 内返回 REJECT，不卡死）。raw 模式下 Ctrl+C 作按键被本应用捕获为拒绝；TUI 流式期的 SIGINT→cancel 令牌不受影响（确认返回后由循环判定）。
- [x] 确认后菜单帧保留在滚动历史（不清屏、不擦除上一帧）。
  - `coreagent/confirm_ui.py: confirm_interactive` 显式 `erase_when_done=False`、`full_screen=False`；T6 PTY 捕获中两帧菜单（按 ↓ 前后）均留存于输出流，未被擦除/清屏。
- [x] 闪烁定时刷新间隔 = 0.6 秒（读源码常量 / 配置值佐证）。
  - `test_blink_interval_is_point_six`（`CURSOR_BLINK_INTERVAL==0.6`）；`confirm_interactive` 以该常量驱动 `refresh_interval` 与 `_blink()` 的 `asyncio.sleep`。

## T4 非 TTY 降级

- [x] 模拟 stdin/stdout 非 TTY → 走纯文本分支，提示文案精确为 `执行 <name>? [y/N] `，输入非 y → 拒绝，默认（空输入）→ 拒绝。
  - `test_confirm_plain_exact_prompt_and_yes`（`out=="执行 run_command? [y/N] "`、`y`→True）、`test_confirm_plain_non_y_rejects`（`n`→False）、`test_confirm_plain_empty_input_defaults_reject`（EOF→False）。`test_is_interactive_false_for_non_tty`。
- [x] 降级路径不渲染富菜单、不写信任集（断言信任集仍为空）。
  - `tests/test_tui.py::test_non_tty_degrades_no_menu_no_trust`：`is_interactive=False` 时富菜单未被调用（`called["menu"] is False`）、`tui._trusted==set()`。

## T5 接入 + 本会话信任集

- [x] 选「同意，本会话内不再询问」后，**同会话内**同一工具二次写调用不再进入菜单、直接放行（断言第二次未渲染菜单）。
  - `tests/test_tui.py::test_trusted_tool_skips_menu_second_time`：首次 APPROVE_ALWAYS → `run_command` 入信任集；二次直接 True，菜单 `seen==["run_command"]`（只弹一次）。
- [x] 信任按名隔离：记住 `run_command` 后，`edit_file` 仍弹菜单。
  - `test_trust_isolated_by_name`：记住 `run_command` 后 `edit_file` 仍进菜单（`seen==["run_command","edit_file"]`）、返回 False。
- [x] 信任仅内存：操作后 `git status` 无新增配置文件、无落盘（贴 `git status` / 目录对比）。
  - `git status --porcelain` 仅见源码/测试（`coreagent/confirm_ui.py`、`coreagent/tui.py`、`tests/test_*`），无任何新增配置/落盘文件；`confirm_ui` 与 `_confirm` 全程不写文件，信任集为内存 `set`。
- [x] 契约不变：确认对循环仍只回传 bool（同意类→True、拒绝→False）；`coreagent/agent.py` 的 `ConfirmCb` 签名与 `_run_tools` 调用方式未改（贴 diff 证明 agent.py 确认相关行未动）。
  - `test_confirm_returns_bool_contract`：APPROVE→`True`、REJECT→`False`，均 `isinstance bool`，纯「同意」不写信任集。`agent.py` 未改：`grep` 仍为 `ConfirmCb = Callable[[dict], Awaitable[bool]]`（L47）、`approved = await confirm(tc) ...`（L142）；`git status` 中 `agent.py` 为未追踪且**不在**已修改列表（本期零改动）。
- [x] 读类工具（如 `read_file`）全程不触发确认（沿用 #0004 行为）。
  - 既有 `tests/test_agent.py::test_confirm_called_for_write_not_read` 仍绿（读类不进 confirm）；本期未改 `_run_tools` 读/写分类逻辑。

## 单测与回归

- [x] `uv run pytest tests/ -q` 全绿；新增 `tests/test_confirm_ui.py` 覆盖 T1/T2/T4 纯函数与降级。贴命令 + 末尾统计行。
  - `uv run pytest tests/` → `100 passed in 3.73s`；`uv run pytest tests/test_confirm_ui.py` → `25 passed`（T1/T2/T3/T4）。
- [x] 既有 `tests/test_tui.py`、`tests/test_agent.py` 不回归。
  - `uv run pytest tests/test_tui.py tests/test_agent.py` → `24 passed`。`uv run ruff check`（新增/改动文件）→ `All checks passed!`。

## T6 端到端（至少一条）

> 富菜单需 TTY；本环境用真实 **pty**（`/tmp/e2e_driver.py` → `/tmp/e2e_confirm.py`）驱动**真实 `TUI._confirm` → 真实 `confirm_ui` 富菜单 / prompt_toolkit 应用**，确定性复现（不依赖模型）。非 TTY 路径用管道喂入真实进程。

- [x] 真实对话触发写类 → 终端肉眼见：区域标签 + 高亮预览 + 分隔线 + 闪烁问句 + 三选项 + 快捷键条。贴终端片段。
  - PTY 捕获（去 ANSI）：

    ```
    [E2E] is_interactive=True
     命令
    ╭──────────────────╮
    │ echo hello-world │
    ╰──────────────────╯
      将在 shell 中执行命令
    ────────────────────────────────────────────────────────────────────────────────
    是否执行此操作？▌
     › 同意执行
     2 同意，本会话内不再询问 run_command
     3 拒绝

     ↑↓  移动    ↵  确认    esc  取消
    ```
    组件命中检查：区域标签[命令]/选项1·2·3/指示符`›`/快捷键`↑↓`·`↵`·`esc`/预览命令原文 —— 全部 OK。
    `edit_file` 渲染另见 T2：`- return a-b`（红）/`+ return a+b`（绿）+ 路径头 `app.py`；`write_file` 含 python 语法高亮 + 路径头 `m.py`。
- [x] e2e 中选项 2（记住）后，让助手再次调用同一工具 → 不再弹菜单、直接执行。贴片段。
  - 同一 PTY 跑次内，发送「↓ + 回车」选项 2 后：`[E2E] RESULT1=True trusted_run_command=True`；**第二次**同名调用无任何菜单帧，直接 `[E2E] RESULT2=True` → `[E2E] DONE`。
- [x] 非 TTY e2e：把一段输入通过管道喂给程序触发确认 → 确认走 `[y/N]` 文本、进程不报错、不卡死。贴命令 + 输出。
  - `printf 'y\ny\n' | uv run python /tmp/e2e_confirm.py`：

    ```
    Warning: Input is not a terminal (fd=0).
    [E2E] is_interactive=False
    执行 run_command? [y/N] [E2E] RESULT1=True trusted_run_command=False
    执行 run_command? [y/N] [E2E] RESULT2=True
    [E2E] DONE
    ```
    默认拒绝亦验证：`printf '' |`（EOF）与 `printf 'n\nn\n' |` 均 `RESULT1=False`/`RESULT2=False`、`trusted=False`、进程正常退出不卡死。
