import { Modal } from "../common/Modal";
import { actions, useStore } from "../../state/store";

/** 文件内容查看器（#0026 T7）：点文件树文件后拉取内容，纯文本浮层展示。 */
export function FileViewer() {
  const viewer = useStore((s) => s.fileViewer);
  return (
    <Modal
      open={viewer !== null}
      onClose={() => actions.closeFileViewer()}
      title={viewer?.path}
      labelledBy="file-viewer-title"
    >
      <pre className="file-viewer__body scroll-area">{viewer?.content}</pre>
    </Modal>
  );
}
