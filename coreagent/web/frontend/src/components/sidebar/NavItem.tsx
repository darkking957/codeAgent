import type { LucideIcon } from "lucide-react";

interface Props {
  icon: LucideIcon;
  label: string;
  active?: boolean;
  onClick: () => void;
}

/** 侧栏导航项（reference §6.2）：图标 + 文字 + hover/选中态胶囊。 */
export function NavItem({ icon: Icon, label, active = false, onClick }: Props) {
  return (
    <button type="button" className={`nav-item${active ? " is-active" : ""}`} onClick={onClick}>
      <Icon size={18} strokeWidth={1.75} className="nav-item__icon" />
      <span className="nav-item__label">{label}</span>
    </button>
  );
}
