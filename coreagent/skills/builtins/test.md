---
name: test
description: 在独立子对话里运行项目测试并回流一条结果摘要（独立模式）
allowed-tools: [run_command, read_file, grep, glob]
mode: independent
max-history-tokens: 4000
---
你正在执行「测试」技能（独立子对话）。目标：运行项目测试、定位失败、给出一句话结论摘要。

步骤：
1. 先判定测试方式：有 `pytest` 就跑 `pytest -q`；否则按项目约定（`make test` / `npm test` 等）。
2. 用 run_command 运行测试，读取输出。
3. 若有失败：用 read_file / grep 定位失败用例与相关代码，简述根因。
4. 最后用一段话给出**收尾摘要**（这段文字会作为唯一结果回流主对话）：通过/失败数、失败用例与根因、建议下一步。

测试范围（若用户指定）：$ARGUMENTS
