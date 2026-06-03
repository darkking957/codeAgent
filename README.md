# CoreAgent

终端 AI 编程助手，命令行交互，Python 实现。设计目标类似 Claude Code——在终端里与 AI 协作完成编程任务。

## 技术栈

| 组件 | 用途 |
|------|------|
| LangGraph | Agent 编排与状态机 |
| FastAPI | 服务层 / 本地 API |
| RAG | 代码库检索 |
| MCP | 工具与外部上下文接入 |

## 安装

需要 Python ≥ 3.11。

```bash
pip install -e .
```

## 配置

编辑 `config.yaml`：

```yaml
protocol: anthropic      # anthropic | openai
model: claude-sonnet-4-6
base_url: ""             # 留空使用默认地址
api_key: ""              # 或设置环境变量

thinking:
  enabled: false
  budget_tokens: 10000
```

支持任何兼容 Anthropic / OpenAI 协议的服务（如 DeepSeek）。

## 运行

```bash
coreagent
```

或直接：

```bash
python -m coreagent
```

## 目录结构

```
coreagent/          核心源码
specs/              功能规格文档（spec / tasks / checklist）
config.yaml         运行时配置
pyproject.toml      包元数据与依赖
CLAUDE.md           AI 协作工作流说明
```

## 开发工作流

见 [CLAUDE.md](CLAUDE.md)——四步循环：面试 → 写文档 → 实现 → 评审。
