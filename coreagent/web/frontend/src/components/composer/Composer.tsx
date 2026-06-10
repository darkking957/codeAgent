import { useImperativeHandle, useRef, useState, forwardRef } from "react";
import TextareaAutosize from "react-textarea-autosize";

import { ComposerToolbar } from "./ComposerToolbar";
import { AttachChips, type Attachment } from "./Attach";
import { actions, useStore } from "../../state/store";

export interface ComposerHandle {
  /** 把脚手架文本注入输入框并聚焦（快捷动作 chip 用）。 */
  insert: (text: string) => void;
}

/**
 * 输入框（reference §6.8，#0026 T5/T6）：白底大圆角浮起卡片 + autosize 文本域 + 底部工具条。
 * 发送 / 重生成 / 编辑共用；运行中禁输入、发送按钮变停止。
 */
export const Composer = forwardRef<ComposerHandle, { autoFocus?: boolean }>(function Composer(
  { autoFocus = false },
  ref
) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const running = useStore((s) => s.running);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useImperativeHandle(ref, () => ({
    insert: (t: string) => {
      setText((prev) => (prev ? prev + "\n" + t : t));
      taRef.current?.focus();
    },
  }));

  const hasContent = text.trim().length > 0 || attachments.length > 0;

  const submit = () => {
    if (running || !hasContent) return;
    void actions.sendMessage(text, attachments);
    setText("");
    setAttachments([]);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 发送；Shift+Enter / 输入法合成中换行。
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className={`composer${running ? " is-running" : ""}`}>
      <AttachChips attachments={attachments} onChange={setAttachments} />
      <label className="sr-only" htmlFor="composer-input">
        给 CoreAgent 发送消息
      </label>
      <TextareaAutosize
        id="composer-input"
        ref={taRef}
        className="composer__input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="How can I help you today?"
        minRows={1}
        maxRows={12}
        disabled={running}
        autoFocus={autoFocus}
      />
      <ComposerToolbar
        hasContent={hasContent}
        running={running}
        onSend={submit}
        onStop={() => actions.stop()}
        attachments={attachments}
        onAttach={setAttachments}
      />
    </div>
  );
});
