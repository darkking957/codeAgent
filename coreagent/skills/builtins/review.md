---
name: review
description: 评审当前工作区的代码改动，只报影响正确性/需求的问题（接管 /review）
allowed-tools: [run_command, read_file, grep, glob]
mode: shared
---
你正在执行「评审」技能。请评审当前工作区的代码改动：

1. 先用工具查看改动（`git status`、`git diff`），必要时 read_file 读相关文件的完整上下文。
2. 对照需求与上下文逐项检查：正确性、边界场景与异常路径、是否动到范围外。
3. 只报影响正确性 / 需求的问题，每条附「文件:行号」；风格类问题不报。
4. 没问题也请明确说明「未发现影响正确性的问题」。

评审重点（若用户指定）：$ARGUMENTS
