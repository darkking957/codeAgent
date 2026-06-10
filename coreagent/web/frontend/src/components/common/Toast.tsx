import { useStore } from "../../state/store";

/** 轻量错误提示（非阻塞）：store.toast 有值时浮现于底部居中。 */
export function Toast() {
  const toast = useStore((s) => s.toast);
  if (!toast) return null;
  return (
    <div className="toast" role="status" aria-live="polite">
      {toast}
    </div>
  );
}
