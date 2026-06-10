# #0005 富交互执行确认 —— 任务

> 新增模块 `coreagent/confirm_ui.py` 承载预览构建 / 六组件渲染 / 交互应用 / 降级；
> `coreagent/tui.py` 仅做接入与「本会话信任集」。能独立单测的纯函数任务靠前，接入与 e2e 殿后。

---

## T1｜内容预览构建（纯函数）

把一个工具调用映射成「区域标签 + 预览体 + 描述行 + 截断信息」的结构化预览。

- **影响文件**：`coreagent/confirm_ui.py`（新建）
- **依赖**：无
- **参考定位**：
  - `coreagent/tools/run_command.py`（入参 `command`）
  - `coreagent/tools/write_file.py`（入参 `path` / `content`）
  - `coreagent/tools/edit_file.py`（入参 `path` / `old_string` / `new_string`）
- 按工具名分派：命令→命令文本；写文件→路径 + 内容首段；编辑→`old_string`/`new_string` 行级 diff。
- 截断规则与标签文案是「砍掉的具体值」，落 checklist。缺字段 / 异常输入要兜底，不抛裸异常。

## T2｜六组件渲染（纯函数 → 可渲染内容）

把 T1 的预览 + 选项列表 + 分隔线 + 问句（含闪烁态）+ 快捷键提示条组装成可渲染内容。

- **影响文件**：`coreagent/confirm_ui.py`
- **依赖**：T1
- **参考定位**：`coreagent/tui.py` 的 `_STYLE`（line 76）与 rich 用法（`_render_tool`，line 321）
- 签名接受 `(预览, selected_index, cursor_on)`，便于断言：选中项左侧指示符 + 高亮、其余显序号；`cursor_on` 控制问句尾光标字符有无。
- 命令做 shell 高亮、编辑 diff 增删着色、写文件按扩展名高亮——借 rich 实现。

## T3｜内联交互应用（按键 → 三态决策）

基于 prompt_toolkit 非全屏内联模式，把 T2 渲染挂上键绑定与定时刷新。

- **影响文件**：`coreagent/confirm_ui.py`
- **依赖**：T2
- 键绑定：↑↓（回绕）/ 数字 1·2·3 直选 / 回车确认 / esc / Ctrl+C；返回三态决策（同意 / 同意且记住 / 拒绝），esc 与 Ctrl+C 归拒绝。
- 定时刷新驱动光标闪烁；确认后不擦除（留痕滚动历史）。
- 注意与 #0004 流式期间装的 SIGINT→cancel 处理共存：Ctrl+C 进入本应用需能退出为「拒绝」，不可卡死。

## T4｜非 TTY 降级（纯文本 y/N）

入口处检测非交互终端，回落纯文本确认。

- **影响文件**：`coreagent/confirm_ui.py`
- **依赖**：T1
- 非 TTY → 纯文本提示（默认拒绝），不渲染富 UI、不记忆。提示文案是「砍掉的具体值」，落 checklist。

## T5｜接入主流程 + 本会话信任集（TUI）

重写确认环节调用内联确认；维护「本会话已放行工具集」；三态收敛为二态回传循环。

- **影响文件**：`coreagent/tui.py`
- **依赖**：T3、T4
- **参考定位**：
  - `coreagent/tui.py` `_confirm`（line 347，现 y/N 实现，整体替换）
  - `coreagent/agent.py` confirm 调用点（line 142）与 `ConfirmCb` 类型（line 47，**契约不变**：仍 `dict -> bool`）
- 流程：非 TTY → 降级；工具名已信任 → 直接同意；否则弹菜单。选「同意且记住」→ 把工具名加入信任集（内存、按名、不落盘）后回传同意。
- 主输入框提示符复位问题（line 94 注释、line 395）：新菜单不复用主 `session`，避免再污染 `self.message`。

## T6｜端到端验证

真实跑通并肉眼核对全部组件与三条路径。

- **影响文件**：无（验证）
- **依赖**：T5
- 用 e2e 环境（DeepSeek 的 Anthropic 端点，见项目记忆）发起含写类调用的对话；核对六组件 + 三选项 + 闪烁 + 「记住后二次不弹窗」+ 非 TTY 降级。贴终端片段 / 截图为证。
