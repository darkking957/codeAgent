import { useRef, useState } from "react";
import { MoreHorizontal, Trash2 } from "lucide-react";

import { actions, useStore, shallowEqual } from "../../state/store";
import type { ConversationSummary } from "../../api/types";
import { useDismiss } from "../../hooks/useDismiss";

/** Recents 列表（reference §6.5）：单行截断、滚动、hover ⋯ 删除；query 客户端过滤。 */
export function RecentsList({ query }: { query: string }) {
  const conversations = useStore((s) => s.conversations, shallowEqual);
  const currentId = useStore((s) => s.currentId);

  const q = query.trim().toLowerCase();
  const items = q
    ? conversations.filter((c) => (c.title || "新会话").toLowerCase().includes(q))
    : conversations;

  if (items.length === 0) {
    return <div className="recents-empty">{q ? "无匹配会话" : "暂无会话"}</div>;
  }

  return (
    <ul className="recents">
      {items.map((c) => (
        <RecentItem key={c.id} conv={c} active={c.id === currentId} />
      ))}
    </ul>
  );
}

function RecentItem({ conv, active }: { conv: ConversationSummary; active: boolean }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const ref = useRef<HTMLLIElement>(null);
  useDismiss(ref, menuOpen, () => setMenuOpen(false));

  return (
    <li ref={ref} className={`recent-item${active ? " is-active" : ""}`}>
      <button
        type="button"
        className="recent-item__link"
        onClick={() => actions.selectConversation(conv.id)}
        title={conv.title || "新会话"}
      >
        <span className="recent-item__title">{conv.title || "新会话"}</span>
      </button>
      <button
        type="button"
        className="recent-item__more"
        aria-label="会话操作"
        onClick={() => setMenuOpen((v) => !v)}
      >
        <MoreHorizontal size={16} strokeWidth={1.75} />
      </button>
      {menuOpen && (
        <div className="menu menu--recent" role="menu">
          <button
            type="button"
            role="menuitem"
            className="menu__item menu__item--danger"
            onClick={() => {
              setMenuOpen(false);
              void actions.deleteConversation(conv.id);
            }}
          >
            <Trash2 size={15} strokeWidth={1.75} />
            删除会话
          </button>
        </div>
      )}
    </li>
  );
}
