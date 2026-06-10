import type { ReactNode } from "react";

/** 分区标题（reference §6.3）：12px / muted / 600 / 字距微展。 */
export function SectionLabel({ children }: { children: ReactNode }) {
  return <div className="section-label">{children}</div>;
}
