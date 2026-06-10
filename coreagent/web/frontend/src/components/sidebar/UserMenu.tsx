import { useRef, useState } from "react";
import { ChevronsUpDown, LogOut, Moon, Sun } from "lucide-react";

import { actions, useStore } from "../../state/store";
import { useDismiss } from "../../hooks/useDismiss";

/** 底部用户档案（reference §6.6）：深底白缩写头像 + 蓝点 + 用户名 + 账户菜单（主题 / 登出）。 */
export function UserMenu() {
  const userId = useStore((s) => s.auth.userId);
  const theme = useStore((s) => s.theme);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, open, () => setOpen(false));

  const short = userId ? userId.slice(0, 8) : "";
  const initials = (userId ?? "?").slice(0, 2).toUpperCase();

  return (
    <div className="usermenu" ref={ref}>
      {open && (
        <div className="menu menu--user" role="menu">
          <button
            type="button"
            role="menuitem"
            className="menu__item"
            onClick={() => {
              actions.setTheme(theme === "dark" ? "light" : "dark");
            }}
          >
            {theme === "dark" ? <Sun size={16} strokeWidth={1.75} /> : <Moon size={16} strokeWidth={1.75} />}
            {theme === "dark" ? "切换到亮色" : "切换到暗色"}
          </button>
          <button
            type="button"
            role="menuitem"
            className="menu__item menu__item--danger"
            onClick={() => {
              setOpen(false);
              void actions.logout();
            }}
          >
            <LogOut size={16} strokeWidth={1.75} />
            登出
          </button>
        </div>
      )}
      <button
        type="button"
        className="usermenu__trigger"
        onClick={() => setOpen((v) => !v)}
        aria-label="账户菜单"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="avatar">
          {initials}
          <span className="avatar__dot" aria-hidden="true" />
        </span>
        <span className="usermenu__text">
          <span className="usermenu__name">用户 {short}</span>
          <span className="usermenu__plan">Pro plan</span>
        </span>
        <ChevronsUpDown size={16} strokeWidth={1.75} className="usermenu__chev" />
      </button>
    </div>
  );
}
