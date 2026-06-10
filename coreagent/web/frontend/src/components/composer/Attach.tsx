import { useRef } from "react";
import { Plus, X } from "lucide-react";

import { IconButton } from "../common/IconButton";

export interface Attachment {
  name: string;
  content: string;
}

const IMG_REJECT = "暂不支持图片附件（当前对话模型无视觉能力）";

/** 附件加号按钮（reference §6.8 工具条左侧）：选文本/代码文件 → 读为文本入上下文；图片拒绝。 */
export function AttachButton({
  attachments,
  onChange,
  disabled,
}: {
  attachments: Attachment[];
  onChange: (next: Attachment[]) => void;
  disabled?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  const onPick = (ev: React.ChangeEvent<HTMLInputElement>) => {
    const files = ev.target.files;
    if (!files) return;
    const collected: Attachment[] = [];
    let pending = files.length;
    const finish = () => {
      if (--pending === 0 && collected.length) onChange([...attachments, ...collected]);
    };
    for (const f of Array.from(files)) {
      if (f.type.startsWith("image/")) {
        alert(IMG_REJECT);
        finish();
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => {
        collected.push({ name: f.name, content: String(reader.result ?? "") });
        finish();
      };
      reader.onerror = finish;
      reader.readAsText(f);
    }
    ev.target.value = ""; // 允许重复选同名文件
  };

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        style={{ display: "none" }}
        onChange={onPick}
        aria-hidden="true"
        tabIndex={-1}
      />
      <IconButton
        label="添加附件（文本 / 代码文件）"
        className="composer__attach"
        disabled={disabled}
        onClick={() => inputRef.current?.click()}
      >
        <Plus size={20} strokeWidth={1.75} />
      </IconButton>
    </>
  );
}

/** 已选附件的胶囊列表（输入框上方）。 */
export function AttachChips({
  attachments,
  onChange,
}: {
  attachments: Attachment[];
  onChange: (next: Attachment[]) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <div className="attach-chips">
      {attachments.map((a, i) => (
        <span key={i} className="attach-chip" title={a.name}>
          <span className="attach-chip__name">{a.name}</span>
          <button
            type="button"
            className="attach-chip__rm"
            aria-label={`移除附件 ${a.name}`}
            onClick={() => onChange(attachments.filter((_, j) => j !== i))}
          >
            <X size={13} strokeWidth={2} />
          </button>
        </span>
      ))}
    </div>
  );
}
