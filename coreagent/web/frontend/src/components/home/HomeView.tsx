import { useRef } from "react";

import { Greeting } from "./Greeting";
import { QuickActions } from "./QuickActions";
import { Composer, type ComposerHandle } from "../composer/Composer";

/** 首页空态（reference §5/§6.7-6.9）：居中偏上的欢迎语 + 输入框 + 快捷动作。 */
export function HomeView() {
  const composerRef = useRef<ComposerHandle>(null);

  return (
    <div className="home-view scroll-area">
      <div className="home-view__col">
        <Greeting />
        <Composer ref={composerRef} autoFocus />
        <QuickActions onInsert={(t) => composerRef.current?.insert(t)} />
      </div>
    </div>
  );
}
