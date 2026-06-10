import { useEffect } from "react";

import { AppLayout } from "./components/AppLayout";
import { AuthView } from "./components/auth/AuthView";
import { Toast } from "./components/common/Toast";
import { actions, useStore } from "./state/store";
import { watchSystemTheme } from "./theme";

export function App() {
  const status = useStore((s) => s.auth.status);

  // 启动：解析当前用户 → 切 Auth / 工作区。
  useEffect(() => {
    void actions.init();
  }, []);

  // 视口宽度 → 窄屏（抽屉式侧栏）判定。
  useEffect(() => {
    const onResize = () => actions.setNarrow(window.innerWidth < 768);
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // 未手动选择主题时跟随系统切换。
  useEffect(() => watchSystemTheme((t) => actions.syncTheme(t)), []);

  return (
    <>
      {status === "loading" ? (
        <div className="boot-splash" aria-busy="true" />
      ) : status === "anon" ? (
        <AuthView />
      ) : (
        <AppLayout />
      )}
      <Toast />
    </>
  );
}
