import { useEffect, useRef } from "react";
import { PanelLeft, Search, X } from "lucide-react";

import { IconButton } from "../common/IconButton";
import { actions, useStore } from "../../state/store";

interface Props {
  searchOpen: boolean;
  query: string;
  onToggleSearch: () => void;
  onQueryChange: (q: string) => void;
}

/** 侧栏顶部：衬线字标 + 搜索（客户端过滤 Recents）+ 收起侧栏（reference §6.1）。 */
export function SidebarHeader({ searchOpen, query, onToggleSearch, onQueryChange }: Props) {
  const isNarrow = useStore((s) => s.isNarrow);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (searchOpen) inputRef.current?.focus();
  }, [searchOpen]);

  return (
    <div className="sidebar-header">
      <div className="sidebar-header__row">
        <span className="wordmark serif">CoreAgent</span>
        <div className="sidebar-header__actions">
          <IconButton label="搜索会话" size="sm" onClick={onToggleSearch} active={searchOpen}>
            <Search size={18} strokeWidth={1.75} />
          </IconButton>
          <IconButton
            label="收起侧栏"
            size="sm"
            onClick={() => (isNarrow ? actions.closeDrawer() : actions.toggleSidebar())}
          >
            <PanelLeft size={18} strokeWidth={1.75} />
          </IconButton>
        </div>
      </div>
      {searchOpen && (
        <div className="sidebar-search">
          <Search size={15} strokeWidth={1.75} className="sidebar-search__icon" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="搜索最近会话…"
            aria-label="搜索最近会话"
          />
          {query && (
            <IconButton label="清除搜索" size="sm" onClick={() => onQueryChange("")}>
              <X size={15} strokeWidth={1.75} />
            </IconButton>
          )}
        </div>
      )}
    </div>
  );
}
