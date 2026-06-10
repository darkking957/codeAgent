"""MCP 客户端固定值（#0008 checklist「固定值表」集中落地）。

数值取权威来源（MCP 规范 2025-11-25 / MCP TypeScript SDK / Claude Code MCP 文档）。
集中一处便于审计与维护；各层从此导入，不散落魔数。
"""

# ── 协议版本 ─────────────────────────────────────────────────────────────────
# client 通告**最新**版本；协商会回落到 server 支持的版本（见 MCP 规范 lifecycle）。
PROTOCOL_VERSION = "2025-11-25"
# client 能兼容的版本集合（含历史版本，供回落）；server 回不在集合内的版本 → 终止连接。
SUPPORTED_PROTOCOL_VERSIONS = {
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
}
CLIENT_NAME = "CoreAgent"
CLIENT_VERSION = "0.1.0"
# client 声明的 capabilities（瘦 v1 不声明 roots / sampling 等高级特性）。
CLIENT_CAPABILITIES: dict = {}
# 握手完成通知 method（见 checklist 固定值）。
INITIALIZED_NOTIFICATION = "notifications/initialized"

# ── 超时（秒）──────────────────────────────────────────────────────────────────
# 连接 + 握手超时（对齐 Claude Code MCP_TIMEOUT 示例；慢启动子进程放宽到 10s）。
CONNECT_TIMEOUT = 10.0
# 单请求超时 / 工具调用超时（对齐 MCP TS SDK DEFAULT_REQUEST_TIMEOUT_MSEC=60000）。
REQUEST_TIMEOUT = 60.0
TOOL_TIMEOUT = 60.0

# ── 重启 ─────────────────────────────────────────────────────────────────────
# stdio 连接 / 进程级重启上限（对齐 Claude Code 初连「up to three times」瞬态重试）。
MAX_RESTARTS = 3

# ── 输出截断 ─────────────────────────────────────────────────────────────────
# 软上限：超出即截断（≈25,000 tokens × 4 chars/token，对齐 Claude Code MAX_MCP_OUTPUT_TOKENS）。
OUTPUT_LIMIT = 100_000
# 绝对硬顶（对齐 Claude Code anthropic/maxResultSizeChars）：防御超大串占内存。
OUTPUT_HARD_CAP = 500_000
# 截断尾注文案（见 checklist 固定值，精确）。
TRUNCATE_SUFFIX = "…（输出已截断，超过 100000 字符上限）"

# ── 启用持久化 ───────────────────────────────────────────────────────────────
# 项目级 server 首次启用批准后写入 local 作用域的键名（见 checklist 固定值）。
ENABLED_KEY = "enabledMcpServers"


def tool_full_name(server: str, tool: str) -> str:
    """工具名前缀格式：``mcp__<server>__<tool>``（双下划线分隔，对齐 Claude Code 命名约定）。"""
    return f"mcp__{server}__{tool}"


def truncate_output(text: str) -> str:
    """工具输出截断：超 OUTPUT_LIMIT 即截断并加尾注；先硬顶切防超大串。"""
    if len(text) > OUTPUT_HARD_CAP:
        text = text[:OUTPUT_HARD_CAP]
    if len(text) > OUTPUT_LIMIT:
        return text[:OUTPUT_LIMIT] + TRUNCATE_SUFFIX
    return text
