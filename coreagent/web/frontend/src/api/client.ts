// REST 客户端（#0026）：同源 fetch，Cookie 自动随行（鉴权 #0025）。仅消费现有端点，不改契约。

import type {
  ApprovalDecision,
  ConversationDetail,
  ConversationSummary,
  FileNode,
  Mode,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string
  ) {
    super(detail);
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    credentials: "same-origin",
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* 非 JSON 错误体：保留默认文案 */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

// —— 鉴权 ——
export const api = {
  me: () => request<{ id: string }>("/auth/me"),
  sendCode: (phone: string) =>
    request<{ sent: boolean; code?: string }>("/auth/send-code", {
      method: "POST",
      body: JSON.stringify({ phone }),
    }),
  register: (phone: string, code: string, password: string) =>
    request<{ id: string }>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ phone, code, password }),
    }),
  login: (phone: string, password: string) =>
    request<{ id: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ phone, password }),
    }),
  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),

  // —— 会话 ——
  listConversations: () => request<ConversationSummary[]>("/conversations"),
  createConversation: (mode: Mode) =>
    request<{ id: string; mode: Mode; workspace: string }>("/conversations", {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  getConversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),
  deleteConversation: (id: string) =>
    request<{ id: string; deleted: boolean }>(`/conversations/${id}`, { method: "DELETE" }),

  // —— 审批回传 ——
  decideApproval: (id: string, toolId: string, decision: ApprovalDecision) =>
    request<unknown>(`/conversations/${id}/approvals/${toolId}`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),

  // —— 文件浏览（code 模式）——
  listFiles: (id: string) =>
    request<{ root: string; tree: FileNode[] }>(`/conversations/${id}/files`),
  fileContent: (id: string, path: string) =>
    request<{ path: string; content: string }>(
      `/conversations/${id}/files/content?path=${encodeURIComponent(path)}`
    ),

  // —— 用量 ——
  usage: () =>
    request<{ input_tokens: number; output_tokens: number; requests: number }>("/usage"),
};
