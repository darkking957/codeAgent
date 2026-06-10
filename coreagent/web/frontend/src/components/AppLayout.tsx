import { Sidebar } from "./sidebar/Sidebar";
import { MainArea } from "./MainArea";
import { actions, useStore } from "../state/store";

/**
 * 双栏外壳（reference §5/§9）：左侧栏 + 右主区，之间发丝竖线。
 * 宽屏固定双栏（可收起）；窄屏侧栏退化为带遮罩的覆盖式抽屉。
 */
export function AppLayout() {
  const collapsed = useStore((s) => s.sidebarCollapsed);
  const drawerOpen = useStore((s) => s.drawerOpen);
  const isNarrow = useStore((s) => s.isNarrow);

  const showSidebar = isNarrow ? drawerOpen : !collapsed;

  return (
    <div className={`app-layout${isNarrow ? " is-narrow" : ""}`}>
      {showSidebar && (
        <aside className={`sidebar-host${isNarrow ? " is-drawer" : ""}`}>
          <Sidebar />
        </aside>
      )}
      {isNarrow && drawerOpen && (
        <div className="drawer-scrim" onClick={() => actions.closeDrawer()} aria-hidden="true" />
      )}
      <MainArea sidebarHidden={!showSidebar} />
    </div>
  );
}
