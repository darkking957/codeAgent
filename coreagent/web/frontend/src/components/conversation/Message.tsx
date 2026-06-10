import { useState } from "react";
import { Check, Copy, Paperclip, Pencil, RefreshCw, X } from "lucide-react";
import TextareaAutosize from "react-textarea-autosize";

import { SearchSources, ToolResultCard, ToolUseCard } from "./ToolCard";
import { actions, useStore } from "../../state/store";
import type { ChatMessage, Citation, ContentBlock, TextBlock } from "../../api/types";

/** assistant 富文本气泡：有后端净化 HTML → 注入（前端不二次跑 markdown）；否则纯文本兜底。 */
function RichText({ html, raw }: { html?: string; raw: string }) {
  if (html != null) {
    // 后端白名单清洗结果（render.py），前端不二次放宽（spec 安全要求）。
    return <div className="bubble" dangerouslySetInnerHTML={{ __html: html }} />;
  }
  return <div className="bubble raw">{raw}</div>;
}

function Citations({ citations }: { citations: Citation[] }) {
  return (
    <div className="citations">
      <span>引用：</span>
      {citations.map((c, i) => (
        <a key={i} href={c.url || "#"} target="_blank" rel="noopener noreferrer">
          [{i + 1}] {c.title || c.url || ""}
        </a>
      ))}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="msg-action"
      aria-label="复制原始文本"
      onClick={() => {
        navigator.clipboard.writeText(text).then(
          () => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1200);
          },
          () => undefined
        );
      }}
    >
      {copied ? <Check size={14} strokeWidth={1.75} /> : <Copy size={14} strokeWidth={1.75} />}
      {copied ? "已复制" : "复制"}
    </button>
  );
}

// —— 用户消息：把附件包裹标签拆成文件胶囊 + 正文（与后端存储格式对应）——
const ATTACH_RE = /<attachment name="([^"]*)">\n([\s\S]*?)\n<\/attachment>\n\n/g;
function parseUserContent(content: string): { names: string[]; text: string } {
  const names: string[] = [];
  const text = content.replace(ATTACH_RE, (_m, name: string) => {
    names.push(name);
    return "";
  });
  return { names, text };
}

function UserMessage({ message, index }: { message: ChatMessage; index: number }) {
  const running = useStore((s) => s.running);
  const [editing, setEditing] = useState(false);
  const raw = typeof message.content === "string" ? message.content : "";
  const { names, text } = parseUserContent(raw);
  const [draft, setDraft] = useState(text);

  if (editing) {
    return (
      <div className="msg msg--user msg--editing">
        <TextareaAutosize
          className="msg-edit__input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          minRows={1}
          maxRows={16}
          autoFocus
        />
        <div className="msg-edit__actions">
          <button type="button" className="btn btn--ghost" onClick={() => setEditing(false)}>
            <X size={14} strokeWidth={1.75} /> 取消
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => {
              setEditing(false);
              void actions.editMessage(index, draft);
            }}
          >
            发送
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="msg msg--user">
      <div className="msg--user__body">
        {names.length > 0 && (
          <div className="msg-attachments">
            {names.map((n, i) => (
              <span key={i} className="attach-chip attach-chip--static" title={n}>
                <Paperclip size={12} strokeWidth={1.75} />
                <span className="attach-chip__name">{n}</span>
              </span>
            ))}
          </div>
        )}
        {text && <div className="user-bubble">{text}</div>}
      </div>
      <div className="msg-actions">
        <button
          type="button"
          className="msg-action"
          aria-label="编辑并重发"
          disabled={running}
          onClick={() => {
            setDraft(text);
            setEditing(true);
          }}
        >
          <Pencil size={14} strokeWidth={1.75} /> 编辑
        </button>
      </div>
    </div>
  );
}

function assistantRawText(content: string | ContentBlock[]): string {
  if (typeof content === "string") return content;
  return content
    .filter((b): b is TextBlock => b.type === "text")
    .map((b) => b.text || "")
    .join("\n\n");
}

function AssistantMessage({ message }: { message: ChatMessage }) {
  const running = useStore((s) => s.running);
  const mode = useStore((s) => s.currentMode);
  const content = message.content;

  return (
    <div className="msg msg--asst">
      <div className="msg--asst__body">
        {typeof content === "string" ? (
          <RichText html={message.html} raw={content} />
        ) : (
          content.map((block, i) => {
            switch (block.type) {
              case "text":
                return (
                  <div key={i}>
                    <RichText html={block.html} raw={block.text || ""} />
                    {block.citations && block.citations.length > 0 && (
                      <Citations citations={block.citations} />
                    )}
                  </div>
                );
              case "thinking":
                return (
                  <div key={i} className="thinking">
                    {block.thinking}
                  </div>
                );
              case "tool_use":
              case "server_tool_use":
                return (
                  <ToolUseCard
                    key={i}
                    name={block.name}
                    input={block.input ?? {}}
                    mode={mode}
                  />
                );
              case "web_search_tool_result":
                return Array.isArray(block.content) ? (
                  <div key={i} className="tool-card">
                    <div className="tool-card__head">
                      <span className="tool-card__title">🔎 搜索结果</span>
                    </div>
                    <SearchSources sources={block.content} />
                  </div>
                ) : (
                  <div key={i} className="status-bad">
                    ✗ 搜索失败：{block.content?.error_code || ""}
                  </div>
                );
              default:
                return null;
            }
          })
        )}
      </div>
      <div className="msg-actions">
        <CopyButton text={assistantRawText(content)} />
        <button
          type="button"
          className="msg-action"
          aria-label="重新生成"
          disabled={running}
          onClick={() => void actions.regenerate()}
        >
          <RefreshCw size={14} strokeWidth={1.75} /> 重新生成
        </button>
      </div>
    </div>
  );
}

/** 一条存储消息：按角色 / content 形态分派渲染。 */
export function Message({ message, index }: { message: ChatMessage; index: number }) {
  if (message.role === "user" && typeof message.content === "string") {
    return <UserMessage message={message} index={index} />;
  }
  if (message.role === "assistant") {
    return <AssistantMessage message={message} />;
  }
  if (message.role === "user" && Array.isArray(message.content)) {
    // 承载 tool_result 的 user 消息 → 结果卡。
    return (
      <div className="msg msg--tool">
        {message.content.map((block, i) =>
          block.type === "tool_result" ? (
            <ToolResultCard key={i} content={block.content} isError={!!block.is_error} />
          ) : null
        )}
      </div>
    );
  }
  return null;
}
