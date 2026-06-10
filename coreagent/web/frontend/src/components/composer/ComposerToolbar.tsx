import { ArrowUp, ChevronDown, Code2, MessageCircle, Square } from "lucide-react";

import { AttachButton, type Attachment } from "./Attach";
import { actions, useStore } from "../../state/store";

// 模型 / 推理强度：只读展示当前配置（#0026 checklist C；后端不支持运行时切换，故不接线为真功能）。
const MODEL_LABEL = "Opus 4.8";
const REASONING_LABEL = "High";

interface Props {
  hasContent: boolean;
  running: boolean;
  onSend: () => void;
  onStop: () => void;
  attachments: Attachment[];
  onAttach: (next: Attachment[]) => void;
}

/** 输入框底部工具条（reference §6.8）：左 + 附件 / 模式；右 模型→推理（只读）→ 发送·停止。 */
export function ComposerToolbar({ hasContent, running, onSend, onStop, attachments, onAttach }: Props) {
  const currentId = useStore((s) => s.currentId);
  const draftMode = useStore((s) => s.draftMode);
  const currentMode = useStore((s) => s.currentMode);
  const isHome = currentId === null;
  const mode = isHome ? draftMode : currentMode;

  return (
    <div className="composer-toolbar">
      <div className="composer-toolbar__left">
        <AttachButton attachments={attachments} onChange={onAttach} disabled={running} />
        {isHome ? (
          // 首页空态：模式可切（决定将创建的会话类型，映射真实 chat/code 能力）。
          <button
            type="button"
            className="mode-pill"
            onClick={() => actions.setDraftMode(mode === "chat" ? "code" : "chat")}
            aria-label={`对话模式：${mode === "code" ? "Code" : "Chat"}（点击切换）`}
          >
            {mode === "code" ? (
              <Code2 size={15} strokeWidth={1.75} />
            ) : (
              <MessageCircle size={15} strokeWidth={1.75} />
            )}
            {mode === "code" ? "Code" : "Chat"}
          </button>
        ) : (
          // 会话内：模式随会话固定，只读展示。
          <span className="mode-pill mode-pill--readonly">
            {mode === "code" ? (
              <Code2 size={15} strokeWidth={1.75} />
            ) : (
              <MessageCircle size={15} strokeWidth={1.75} />
            )}
            {mode === "code" ? "Code" : "Chat"}
          </span>
        )}
      </div>

      <div className="composer-toolbar__right">
        <span className="readonly-pill" aria-label={`模型 ${MODEL_LABEL}`}>
          {MODEL_LABEL}
        </span>
        <span className="readonly-pill" aria-label={`推理强度 ${REASONING_LABEL}`}>
          {REASONING_LABEL}
          <ChevronDown size={14} strokeWidth={1.75} className="readonly-pill__chev" />
        </span>
        {running ? (
          <button type="button" className="send-btn send-btn--stop" onClick={onStop} aria-label="停止生成">
            <Square size={16} strokeWidth={2.5} fill="currentColor" />
          </button>
        ) : (
          <button
            type="button"
            className={`send-btn${hasContent ? " is-ready" : ""}`}
            onClick={onSend}
            disabled={!hasContent}
            aria-label="发送"
          >
            <ArrowUp size={18} strokeWidth={2.25} />
          </button>
        )}
      </div>
    </div>
  );
}
