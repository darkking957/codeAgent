// 轻量全局 store（#0026 T3）：零依赖、基于 useSyncExternalStore，按 selector 选择性订阅，
// 避免流式逐字更新触发整树重渲染。状态 + 全部动作（orchestration）集中于此。

import { useRef, useSyncExternalStore } from "react";

import { ApiError, api } from "../api/client";
import { streamMessages, type StreamHandle } from "../api/stream";
import type {
  ApprovalDecision,
  ChatMessage,
  ConversationSummary,
  FileNode,
  Mode,
  SSEFrame,
} from "../api/types";
import { applyTheme, currentTheme, setTheme as persistTheme, type Theme } from "../theme";

// —— 流式 live 渲染缓冲：一轮 assistant 的有序条目（text/thinking 与工具卡交错）——
export type LiveItem =
  | { kind: "text"; text: string }
  | { kind: "thinking"; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      input: Record<string, unknown>;
      result?: { content: string; isError: boolean; rejected: boolean };
    }
  | { kind: "diff"; path: string; diff: string }
  | { kind: "retry"; attempt: number; wait: number }
  | { kind: "error"; errorType: string };

export interface LiveState {
  items: LiveItem[];
}

export interface ApprovalState {
  toolId: string;
  name: string;
  input: Record<string, unknown>;
}

export interface AppState {
  auth: { status: "loading" | "anon" | "authed"; userId: string | null };
  theme: Theme;
  sidebarCollapsed: boolean; // 桌面收起
  drawerOpen: boolean; // 窄屏抽屉
  isNarrow: boolean; // 视口 < 768px

  conversations: ConversationSummary[];
  currentId: string | null;
  currentMode: Mode; // 当前会话模式（currentId 为空时即 draftMode 的镜像）
  draftMode: Mode; // 从首页空态新建会话时使用的模式
  messages: ChatMessage[]; // 当前会话权威消息
  loadingConversation: boolean;

  running: boolean;
  live: LiveState | null;

  approval: ApprovalState | null;

  files: { root: string; tree: FileNode[] } | null;
  filesLoading: boolean;
  filesPanelOpen: boolean; // code 模式右侧文件树面板开关
  fileViewer: { path: string; content: string } | null;

  toast: string | null;
}

const initialState: AppState = {
  auth: { status: "loading", userId: null },
  theme: currentTheme(),
  sidebarCollapsed: false,
  drawerOpen: false,
  isNarrow: typeof window !== "undefined" ? window.innerWidth < 768 : false,
  conversations: [],
  currentId: null,
  currentMode: "chat",
  draftMode: "chat",
  messages: [],
  loadingConversation: false,
  running: false,
  live: null,
  approval: null,
  files: null,
  filesLoading: false,
  filesPanelOpen: true,
  fileViewer: null,
  toast: null,
};

// —— 极简 store 内核 ——
let state = initialState;
const listeners = new Set<() => void>();

function getState(): AppState {
  return state;
}
function setState(patch: Partial<AppState> | ((s: AppState) => Partial<AppState>)): void {
  const p = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...p };
  listeners.forEach((l) => l());
}
function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export const store = { getState, setState, subscribe };

/** 选择性订阅 store 切片；selector 返回新对象时用 equality 防抖（默认 Object.is）。 */
export function useStore<U>(
  selector: (s: AppState) => U,
  equality: (a: U, b: U) => boolean = Object.is
): U {
  const last = useRef<{ value: U } | null>(null);
  const getSnapshot = () => {
    const next = selector(getState());
    if (last.current && equality(last.current.value, next)) return last.current.value;
    last.current = { value: next };
    return next;
  };
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

export function shallowEqual<T extends object>(a: T, b: T): boolean {
  if (Object.is(a, b)) return true;
  const ka = Object.keys(a) as (keyof T)[];
  const kb = Object.keys(b) as (keyof T)[];
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.is(a[k], b[k]));
}

// ════════════════════════════════════════════════════════════════════════════════
// 动作（orchestration）
// ════════════════════════════════════════════════════════════════════════════════

let activeStream: StreamHandle | null = null;

function showToast(msg: string): void {
  setState({ toast: msg });
  window.setTimeout(() => {
    if (getState().toast === msg) setState({ toast: null });
  }, 4000);
}

async function refreshConversations(): Promise<void> {
  try {
    setState({ conversations: await api.listConversations() });
  } catch {
    /* 列表刷新失败静默（不阻塞主流程） */
  }
}

async function loadFiles(): Promise<void> {
  const { currentId, currentMode } = getState();
  if (!currentId || currentMode !== "code") {
    setState({ files: null });
    return;
  }
  setState({ filesLoading: true });
  try {
    const data = await api.listFiles(currentId);
    setState({ files: data, filesLoading: false });
  } catch {
    setState({ files: null, filesLoading: false });
  }
}

// —— live 缓冲更新（流式逐帧）——
function pushLive(updater: (items: LiveItem[]) => LiveItem[]): void {
  const live = getState().live ?? { items: [] };
  setState({ live: { items: updater([...live.items]) } });
}

function onFrame(frame: SSEFrame): void {
  switch (frame.event) {
    case "text_delta": {
      const kind = frame.data.kind === "thinking" ? "thinking" : "text";
      pushLive((items) => {
        const last = items[items.length - 1];
        if (last && last.kind === kind) {
          items[items.length - 1] = { ...last, text: last.text + frame.data.text };
        } else {
          items.push({ kind, text: frame.data.text });
        }
        return items;
      });
      break;
    }
    case "tool_call": {
      const call = frame.data.call;
      pushLive((items) => {
        items.push({
          kind: "tool",
          id: call.id ?? "",
          name: call.name,
          input: (call.input ?? {}) as Record<string, unknown>,
        });
        return items;
      });
      break;
    }
    case "tool_result": {
      const id = frame.data.call?.id ?? "";
      pushLive((items) => {
        const idx = items.findIndex((it) => it.kind === "tool" && it.id === id);
        if (idx >= 0) {
          const it = items[idx] as Extract<LiveItem, { kind: "tool" }>;
          items[idx] = {
            ...it,
            result: {
              content: frame.data.content,
              isError: frame.data.is_error,
              rejected: frame.data.rejected,
            },
          };
        }
        return items;
      });
      break;
    }
    case "file_diff":
      pushLive((items) => {
        items.push({ kind: "diff", path: frame.data.path, diff: frame.data.diff });
        return items;
      });
      break;
    case "retry":
      pushLive((items) => {
        items.push({ kind: "retry", attempt: frame.data.attempt, wait: frame.data.wait });
        return items;
      });
      break;
    case "run_error":
      pushLive((items) => {
        items.push({ kind: "error", errorType: frame.data.error_type });
        return items;
      });
      break;
    case "approval_request":
      setState({
        approval: {
          toolId: frame.data.tool_id,
          name: frame.data.name,
          input: frame.data.input,
        },
      });
      break;
    case "approval_decided":
      setState((s) =>
        s.approval && s.approval.toolId === frame.data.tool_id ? { approval: null } : {}
      );
      break;
    // turn_start / run_done：无 live 视觉，收尾统一拉权威历史
    default:
      break;
  }
}

async function runStream(url: string, body: unknown): Promise<void> {
  setState({ running: true, live: { items: [] } });
  const handle = streamMessages(url, body, onFrame);
  activeStream = handle;
  await handle.done;
  activeStream = null;
  // 收尾：拉权威历史重渲染（含后端 html 字段）+ 刷文件树 + 刷会话标题。
  const { currentId } = getState();
  if (currentId) {
    try {
      const detail = await api.getConversation(currentId);
      setState({ messages: detail.messages, currentMode: detail.mode });
    } catch {
      /* 拉取失败保留 live 已渲染内容的兜底由 running=false 处理 */
    }
  }
  setState({ running: false, live: null, approval: null });
  await Promise.all([loadFiles(), refreshConversations()]);
}

export const actions = {
  // —— 启动 / 鉴权 ——
  async init(): Promise<void> {
    try {
      const me = await api.me();
      setState({ auth: { status: "authed", userId: me.id } });
      await refreshConversations();
    } catch {
      setState({ auth: { status: "anon", userId: null } });
    }
  },

  async sendCode(phone: string): Promise<{ code?: string }> {
    return api.sendCode(phone);
  },

  async register(phone: string, code: string, password: string): Promise<void> {
    await api.register(phone, code, password);
  },

  async login(phone: string, password: string): Promise<void> {
    const r = await api.login(phone, password);
    setState({ auth: { status: "authed", userId: r.id } });
    await refreshConversations();
  },

  async logout(): Promise<void> {
    try {
      await api.logout();
    } catch {
      /* 忽略 */
    }
    setState({
      auth: { status: "anon", userId: null },
      conversations: [],
      currentId: null,
      messages: [],
      files: null,
      live: null,
      running: false,
    });
  },

  // —— 主题 / 布局 ——
  setTheme(t: Theme): void {
    persistTheme(t);
    setState({ theme: t });
  },
  /** 跟随系统主题变化（未手动选择时）：应用 data-theme 但不持久化。 */
  syncTheme(t: Theme): void {
    applyTheme(t);
    setState({ theme: t });
  },
  toggleSidebar(): void {
    setState((s) => ({ sidebarCollapsed: !s.sidebarCollapsed }));
  },
  openDrawer(): void {
    setState({ drawerOpen: true });
  },
  closeDrawer(): void {
    setState({ drawerOpen: false });
  },
  setNarrow(isNarrow: boolean): void {
    if (getState().isNarrow !== isNarrow) setState({ isNarrow });
  },

  // —— 会话 ——
  setDraftMode(mode: Mode): void {
    setState({ draftMode: mode, currentMode: mode });
  },

  newDraft(mode: Mode): void {
    setState({
      currentId: null,
      currentMode: mode,
      draftMode: mode,
      messages: [],
      files: null,
      live: null,
      fileViewer: null,
      drawerOpen: false,
    });
  },

  async selectConversation(id: string): Promise<void> {
    if (getState().running) return; // 运行中不切换，避免错配
    setState({ loadingConversation: true, drawerOpen: false });
    try {
      const detail = await api.getConversation(id);
      setState({
        currentId: detail.id,
        currentMode: detail.mode,
        messages: detail.messages,
        live: null,
        fileViewer: null,
        loadingConversation: false,
      });
      await loadFiles();
    } catch {
      setState({ loadingConversation: false });
      showToast("会话加载失败");
    }
  },

  async deleteConversation(id: string): Promise<void> {
    try {
      await api.deleteConversation(id);
    } catch {
      showToast("删除失败");
      return;
    }
    if (getState().currentId === id) {
      setState({ currentId: null, messages: [], files: null, live: null });
    }
    await refreshConversations();
  },

  // —— 发送 / 重生成 / 编辑 ——
  async sendMessage(text: string, attachments: { name: string; content: string }[]): Promise<void> {
    if (getState().running) return;
    const content = wrapAttachments(text, attachments);
    if (!content.trim()) return;
    let id = getState().currentId;
    if (!id) {
      // 首页空态首次发送：按 draftMode 建会话再发（claude.ai 风：composer 即新会话入口）。
      try {
        const created = await api.createConversation(getState().draftMode);
        id = created.id;
        setState({ currentId: created.id, currentMode: created.mode });
      } catch (e) {
        showToast(e instanceof ApiError ? e.detail : "新建会话失败");
        return;
      }
    }
    // 乐观追加用户气泡（最终以收尾权威重渲染为准）。
    setState((s) => ({ messages: [...s.messages, { role: "user", content }] }));
    await runStream(`/conversations/${id}/messages`, { content });
  },

  async regenerate(): Promise<void> {
    const { currentId, running } = getState();
    if (!currentId || running) return;
    await runStream(`/conversations/${currentId}/regenerate`, null);
  },

  async editMessage(index: number, content: string): Promise<void> {
    const { currentId, running } = getState();
    if (!currentId || running) return;
    // 乐观截断到该 index 之前 + 追加新用户消息。
    setState((s) => ({ messages: [...s.messages.slice(0, index), { role: "user", content }] }));
    await runStream(`/conversations/${currentId}/messages/${index}/edit`, { content });
  },

  stop(): void {
    activeStream?.abort();
  },

  // —— 审批 ——
  async decideApproval(decision: ApprovalDecision): Promise<void> {
    const { approval, currentId } = getState();
    if (!approval || !currentId) return;
    setState({ approval: null });
    try {
      await api.decideApproval(currentId, approval.toolId, decision);
    } catch {
      /* 审批回传失败：approval_decided 帧或收尾会兜底关闭 */
    }
  },

  // —— 文件 ——
  refreshFiles: loadFiles,
  toggleFilesPanel(): void {
    setState((s) => ({ filesPanelOpen: !s.filesPanelOpen }));
  },
  async openFile(path: string): Promise<void> {
    const { currentId } = getState();
    if (!currentId) return;
    try {
      const data = await api.fileContent(currentId, path);
      setState({ fileViewer: { path, content: data.content } });
    } catch {
      showToast(`无法读取：${path}`);
    }
  },
  closeFileViewer(): void {
    setState({ fileViewer: null });
  },
};

/** 把文本/代码附件包裹后拼到 user 消息文本前（格式钉死，与后端约定一致）。 */
export function wrapAttachments(
  text: string,
  attachments: { name: string; content: string }[]
): string {
  let prefix = "";
  for (const a of attachments) {
    prefix += `<attachment name="${a.name}">\n${a.content}\n</attachment>\n\n`;
  }
  return prefix + text;
}
