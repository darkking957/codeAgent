---
name: commit
description: 查看工作区改动并生成规范的 git 提交（共享模式，提交前会确认）
allowed-tools: [run_command, read_file, grep, glob]
mode: shared
---
你正在执行「提交」技能。目标：把当前工作区的改动整理成一次规范的 git 提交。

步骤：
1. 先用 run_command 跑 `git status` 与 `git diff`（必要时 `git diff --staged`）了解全部改动；不要臆测改了什么。
2. 把改动按主题归纳；若改动跨多个无关主题，提示用户是否拆分为多次提交。
3. 生成简洁的中文提交信息：首行一句话概括「做了什么」（动宾结构、不超过 50 字），需要时空一行写要点。
4. 用 `git add` 选入相关文件、`git commit` 落地（写操作会请你确认；被拒绝则停下说明原因，不要绕过确认）。
5. 提交后跑 `git log -1 --stat` 回显结果，向用户确认提交成功。

附加要求：$ARGUMENTS

注意：不要 `git push`；不可逆操作（强制、重置）一律先说明影响再问。
