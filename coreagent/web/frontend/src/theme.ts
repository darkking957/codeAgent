// 主题：亮 / 暗双主题，跟随系统 + 手动切换 + 持久化（#0026 T1）。
// data-theme 属性挂在 <html>；index.html 的内联脚本在首屏渲染前先据持久化/系统设好以防闪烁。

export type Theme = "light" | "dark";

const STORAGE_KEY = "coreagent-theme";

export function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** 已持久化的手动选择（无 → null，表示跟随系统）。 */
export function savedTheme(): Theme | null {
  const v = localStorage.getItem(STORAGE_KEY);
  return v === "light" || v === "dark" ? v : null;
}

/** 当前生效主题：手动选择优先，否则跟随系统。 */
export function currentTheme(): Theme {
  return savedTheme() ?? systemTheme();
}

export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
}

/** 手动设置主题：落 data-theme + 持久化。 */
export function setTheme(theme: Theme): void {
  applyTheme(theme);
  localStorage.setItem(STORAGE_KEY, theme);
}

/** 在亮/暗之间切换，返回切到的主题。 */
export function toggleTheme(): Theme {
  const next: Theme = currentTheme() === "dark" ? "light" : "dark";
  setTheme(next);
  return next;
}

/** 未手动选择时跟随系统变化。调用方负责在卸载时移除监听。 */
export function watchSystemTheme(onChange: (t: Theme) => void): () => void {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const handler = (e: MediaQueryListEvent) => {
    if (savedTheme() === null) onChange(e.matches ? "dark" : "light");
  };
  mq.addEventListener("change", handler);
  return () => mq.removeEventListener("change", handler);
}
