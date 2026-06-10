import { Code2, Coffee, GraduationCap, Pencil, Sparkle, type LucideIcon } from "lucide-react";

import { actions } from "../../state/store";

interface Action {
  label: string;
  icon: LucideIcon;
  scaffold: string;
  mode?: "code";
}

// 5 个快捷动作（reference §6.9）：点击注入提示脚手架；Code 另切到 code 模式。
const ACTIONS: Action[] = [
  { label: "Write", icon: Pencil, scaffold: "Help me write " },
  { label: "Learn", icon: GraduationCap, scaffold: "Explain " },
  { label: "Code", icon: Code2, scaffold: "Help me write code that ", mode: "code" },
  { label: "Life stuff", icon: Coffee, scaffold: "Give me advice on " },
  { label: "Claude's choice", icon: Sparkle, scaffold: "Surprise me with something interesting." },
];

/** 输入框下方一行描边胶囊：注入脚手架 / 切模式（reference §6.9）。 */
export function QuickActions({ onInsert }: { onInsert: (text: string) => void }) {
  return (
    <div className="quick-actions">
      {ACTIONS.map((a) => (
        <button
          key={a.label}
          type="button"
          className="chip"
          onClick={() => {
            if (a.mode) actions.setDraftMode(a.mode);
            onInsert(a.scaffold);
          }}
        >
          <a.icon size={16} strokeWidth={1.75} className="chip__icon" />
          {a.label}
        </button>
      ))}
    </div>
  );
}
