import { Menu, PanelLeft, PanelRight } from "lucide-react";

import { IconButton } from "./common/IconButton";
import { ThemeToggle } from "./common/ThemeToggle";
import { HomeView } from "./home/HomeView";
import { ConversationView } from "./conversation/ConversationView";
import { ApprovalModal } from "./approval/ApprovalModal";
import { FileViewer } from "./files/FileViewer";
import { actions, useStore } from "../state/store";

/** 右侧主区：顶部细栏（侧栏触发 + code 文件树开关 + 主题）+ 视图（首页空态 / 会话）+ 模态浮层。 */
export function MainArea({ sidebarHidden }: { sidebarHidden: boolean }) {
  const currentId = useStore((s) => s.currentId);
  const currentMode = useStore((s) => s.currentMode);
  const filesPanelOpen = useStore((s) => s.filesPanelOpen);
  const isNarrow = useStore((s) => s.isNarrow);
  const isCode = currentId !== null && currentMode === "code";

  return (
    <main className="main-area">
      <header className="main-topbar">
        {sidebarHidden &&
          (isNarrow ? (
            <IconButton label="打开侧栏" onClick={() => actions.openDrawer()}>
              <Menu size={20} strokeWidth={1.75} />
            </IconButton>
          ) : (
            <IconButton label="展开侧栏" onClick={() => actions.toggleSidebar()}>
              <PanelLeft size={20} strokeWidth={1.75} />
            </IconButton>
          ))}
        <div className="main-topbar__spacer" />
        {isCode && (
          <IconButton
            label={filesPanelOpen ? "隐藏文件树" : "显示文件树"}
            active={filesPanelOpen}
            onClick={() => actions.toggleFilesPanel()}
          >
            <PanelRight size={19} strokeWidth={1.75} />
          </IconButton>
        )}
        <ThemeToggle />
      </header>

      {currentId ? <ConversationView /> : <HomeView />}

      <ApprovalModal />
      <FileViewer />
    </main>
  );
}
