import { AlertTriangle } from "lucide-react";

import { Modal } from "../common/Modal";
import { actions, useStore } from "../../state/store";

/**
 * 人在回路审批浮层（#0026 T8）：展示工具名 + 完整入参，四档决策回传后端。
 * 无 onClose（强制决策）；approval_decided 帧 / 收尾会清空 store.approval 关闭。
 */
export function ApprovalModal() {
  const approval = useStore((s) => s.approval);
  if (!approval) return null;

  const isCommand = approval.name === "run_command";

  return (
    <Modal open title={<>需要审批：{approval.name}</>} labelledBy="approval-title">
      <div className="approval">
        <div className="approval__caption">即将执行的工具入参（命令 / 参数完全可读）：</div>
        <pre className="approval__input scroll-area">
          {JSON.stringify(approval.input ?? {}, null, 2)}
        </pre>
        {isCommand && (
          <div className="approval__note">
            <AlertTriangle size={15} strokeWidth={1.75} />
            <div>
              <div className="approval__note-strong">此命令执行后文件变更不产生预览</div>
              <div>「永久允许」将放行该工具的任意命令（按工具名，粗粒度作用面）。</div>
            </div>
          </div>
        )}
        <div className="approval__actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void actions.decideApproval("once")}
          >
            本次允许
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void actions.decideApproval("session")}
          >
            本会话允许
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void actions.decideApproval("persist")}
          >
            永久允许
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={() => void actions.decideApproval("reject")}
          >
            拒绝
          </button>
        </div>
      </div>
    </Modal>
  );
}
