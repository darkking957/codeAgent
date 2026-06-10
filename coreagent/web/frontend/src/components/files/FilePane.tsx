import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, File, Folder, RefreshCw } from "lucide-react";

import { IconButton } from "../common/IconButton";
import { actions, useStore } from "../../state/store";
import type { FileNode } from "../../api/types";

/** code 会话文件树面板（#0026 T7）：列 workspace 目录树，点文件拉内容到查看器。 */
export function FilePane() {
  const files = useStore((s) => s.files);
  const loading = useStore((s) => s.filesLoading);
  const currentId = useStore((s) => s.currentId);

  // 进入 code 会话时若未加载则拉一次。
  useEffect(() => {
    if (currentId && !files) void actions.refreshFiles();
  }, [currentId, files]);

  return (
    <aside className="file-pane" aria-label="文件树">
      <div className="file-pane__head">
        <span className="file-pane__title">
          <Folder size={15} strokeWidth={1.75} /> Files
        </span>
        <IconButton label="刷新文件树" size="sm" onClick={() => void actions.refreshFiles()}>
          <RefreshCw size={15} strokeWidth={1.75} />
        </IconButton>
      </div>
      <div className="file-pane__tree scroll-area">
        {loading ? (
          <div className="file-pane__hint">读取中…</div>
        ) : !files || files.tree.length === 0 ? (
          <div className="file-pane__hint">空目录</div>
        ) : (
          <FileTree nodes={files.tree} depth={0} />
        )}
      </div>
    </aside>
  );
}

function FileTree({ nodes, depth }: { nodes: FileNode[]; depth: number }) {
  return (
    <ul className="file-tree">
      {nodes.map((n) => (
        <FileTreeNode key={n.path} node={n} depth={depth} />
      ))}
    </ul>
  );
}

function FileTreeNode({ node, depth }: { node: FileNode; depth: number }) {
  const [open, setOpen] = useState(depth < 1);
  const pad = { paddingLeft: 8 + depth * 12 };

  if (node.type === "dir") {
    const hasChildren = !!node.children && node.children.length > 0;
    return (
      <li>
        <button
          type="button"
          className="file-node file-node--dir"
          style={pad}
          onClick={() => setOpen((v) => !v)}
        >
          {hasChildren ? (
            open ? (
              <ChevronDown size={14} strokeWidth={1.75} className="file-node__chev" />
            ) : (
              <ChevronRight size={14} strokeWidth={1.75} className="file-node__chev" />
            )
          ) : (
            <span className="file-node__chev" />
          )}
          <Folder size={14} strokeWidth={1.75} className="file-node__icon" />
          <span className="file-node__name">{node.name}</span>
        </button>
        {open && hasChildren && <FileTree nodes={node.children!} depth={depth + 1} />}
      </li>
    );
  }

  return (
    <li>
      <button
        type="button"
        className="file-node file-node--file"
        style={pad}
        onClick={() => void actions.openFile(node.path)}
        title={node.path}
      >
        <span className="file-node__chev" />
        <File size={14} strokeWidth={1.75} className="file-node__icon" />
        <span className="file-node__name">{node.name}</span>
      </button>
    </li>
  );
}
