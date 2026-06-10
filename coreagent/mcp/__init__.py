"""MCP 客户端包（#0008 瘦 v1）。

分层（下层不依赖上层）：
  jsonrpc   —— JSON-RPC 2.0 三类消息建模 + 编解码（纯函数，无 I/O）
  transport —— 传输接口 + stdio 子进程传输（stdout=JSON-RPC，stderr=日志）
  session   —— 单 server 会话：握手 / id 异步匹配+超时 / 工具发现 / 工具调用 / 断连重启
  config    —— 读分层配置 `mcpServers`（复用 #0007 作用域）
  adapter   —— 远端工具 → 本仓 Tool 包装 + 同步桥
  enablement—— 项目级启用安全门（首次启用确认 + 持久化）
  manager   —— 事件循环线程承载会话 → 连接(fail-soft) → 注册 → 生命周期

对外主入口：``build_mcp_manager(registry, base_dir, confirm_enable)``。
"""

from coreagent.mcp.errors import McpError
from coreagent.mcp.manager import McpManager, build_mcp_manager

__all__ = ["McpError", "McpManager", "build_mcp_manager"]
