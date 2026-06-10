import { Moon, Sun } from "lucide-react";

import { IconButton } from "./IconButton";
import { actions, useStore } from "../../state/store";

/** 主题切换：亮 ⇄ 暗（手动选择持久化）。 */
export function ThemeToggle() {
  const theme = useStore((s) => s.theme);
  const next = theme === "dark" ? "light" : "dark";
  return (
    <IconButton
      label={next === "dark" ? "切换到暗色" : "切换到亮色"}
      onClick={() => actions.setTheme(next)}
    >
      {theme === "dark" ? <Sun size={19} strokeWidth={1.75} /> : <Moon size={19} strokeWidth={1.75} />}
    </IconButton>
  );
}
