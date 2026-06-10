// 后端契约类型（#0026）：与现有 FastAPI 端点 / SSE 帧逐字对齐，前端只消费、不重塑。

export type Mode = "chat" | "code";

export interface ConversationSummary {
  id: string;
  title: string;
  mode: Mode;
  updated_at: number;
}

// —— 存储里的结构化消息块（GET /conversations/{id} 返回；assistant 文本块带 html 字段）——
export interface TextBlock {
  type: "text";
  text: string;
  html?: string;
  citations?: Citation[];
}
export interface ThinkingBlock {
  type: "thinking";
  thinking: string;
}
export interface ToolUseBlock {
  type: "tool_use" | "server_tool_use";
  id?: string;
  name: string;
  input?: Record<string, unknown>;
}
export interface WebSearchResultBlock {
  type: "web_search_tool_result";
  content: Array<{ title?: string; url?: string }> | { error_code?: string };
}
export interface ToolResultBlock {
  type: "tool_result";
  content: unknown;
  is_error?: boolean;
}
export type ContentBlock =
  | TextBlock
  | ThinkingBlock
  | ToolUseBlock
  | WebSearchResultBlock
  | ToolResultBlock;

export interface Citation {
  title?: string;
  url?: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string | ContentBlock[];
  html?: string; // assistant 字符串 content 的渲染 HTML
}

export interface ConversationDetail {
  id: string;
  mode: Mode;
  workspace: string;
  messages: ChatMessage[];
}

// —— 文件树（code 模式）——
export interface FileNode {
  name: string;
  path: string;
  type: "file" | "dir";
  children?: FileNode[];
}

// —— SSE 帧（与 web/sse.py 逐字对齐）——
export type ToolCallPayload = { id?: string; name: string; input?: Record<string, unknown> };

export type SSEFrame =
  | { event: "text_delta"; data: { kind: "text" | "thinking"; text: string } }
  | { event: "tool_call"; data: { call: ToolCallPayload } }
  | {
      event: "tool_result";
      data: { call: ToolCallPayload; content: string; success: boolean; rejected: boolean; is_error: boolean };
    }
  | { event: "turn_start"; data: { round_index: number } }
  | { event: "retry"; data: { attempt: number; wait: number } }
  | {
      event: "run_done";
      data: { reason: string; plan: unknown; input_tokens: number; output_tokens: number };
    }
  | { event: "run_error"; data: { error_type: string } }
  | { event: "approval_request"; data: { tool_id: string; name: string; input: Record<string, unknown> } }
  | { event: "approval_decided"; data: { tool_id: string; decision: string } }
  | { event: "file_diff"; data: { tool_id: string; path: string; diff: string } };

export type ApprovalDecision = "once" | "session" | "persist" | "reject";
