import type { ButtonHTMLAttributes, ReactNode } from "react";

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string; // 无障碍标签（aria-label）+ title，所有图标按钮必填
  children: ReactNode;
  size?: "sm" | "md";
  active?: boolean;
}

/** 图标按钮：方形命中区 + hover 背景 + focus-visible 焦点环 + 必填 aria-label（reference §10）。 */
export function IconButton({
  label,
  children,
  size = "md",
  active = false,
  className = "",
  ...rest
}: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={`icon-btn icon-btn--${size}${active ? " is-active" : ""} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}
