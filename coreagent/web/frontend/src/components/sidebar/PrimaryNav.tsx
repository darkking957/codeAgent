import { Plus } from "lucide-react";

import { NavItem } from "./NavItem";
import { actions, useStore } from "../../state/store";

/**
 * 主导航。**仅映射真实能力**（#0026「无假壳」）：New chat → 新建 chat 会话。
 * claude.ai 的 Projects / Artifacts / Customize 无后端支撑，按 spec 决策省略而非造假壳。
 */
export function PrimaryNav() {
  const onHomeChat = useStore((s) => s.currentId === null && s.draftMode === "chat");
  return (
    <div className="nav-group">
      <NavItem
        icon={Plus}
        label="New chat"
        active={onHomeChat}
        onClick={() => actions.newDraft("chat")}
      />
    </div>
  );
}
