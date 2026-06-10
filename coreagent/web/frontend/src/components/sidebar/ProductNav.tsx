import { Code2 } from "lucide-react";

import { NavItem } from "./NavItem";
import { actions, useStore } from "../../state/store";

/**
 * Products 区（reference §6.4）。**仅** Code（→ 新建 code 会话，映射真实 code 模式 = 文件树 + 工具 +
 * 人在回路审批）。Design 无后端支撑 → 省略（#0026「无假壳」）。
 */
export function ProductNav() {
  const onHomeCode = useStore((s) => s.currentId === null && s.draftMode === "code");
  return (
    <div className="nav-group">
      <NavItem
        icon={Code2}
        label="Code"
        active={onHomeCode}
        onClick={() => actions.newDraft("code")}
      />
    </div>
  );
}
