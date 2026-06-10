import { useEffect, type ReactNode } from "react";

interface ModalProps {
  open: boolean;
  onClose?: () => void; // 提供则点遮罩 / Esc 关闭；审批等强制决策不传
  title?: ReactNode;
  children: ReactNode;
  labelledBy?: string;
}

/** 居中浮层模态：遮罩 + 卡片，Esc / 点遮罩关闭（onClose 提供时），role=dialog。 */
export function Modal({ open, onClose, title, children, labelledBy }: ModalProps) {
  useEffect(() => {
    if (!open || !onClose) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="modal-overlay" onMouseDown={onClose ? () => onClose() : undefined}>
      <div
        className="modal-box scroll-area"
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {title != null && (
          <h3 id={labelledBy} className="modal-title">
            {title}
          </h3>
        )}
        {children}
      </div>
    </div>
  );
}
