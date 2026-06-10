import { useEffect, useRef } from "react";

import { MessageList } from "./MessageList";
import { Composer } from "../composer/Composer";
import { FilePane } from "../files/FilePane";
import { store, useStore } from "../../state/store";

/** 会话视图（#0026 T6）：消息线（自动滚动跟随）+ 底部输入框；code 模式右侧文件树面板。 */
export function ConversationView() {
  const currentMode = useStore((s) => s.currentMode);
  const filesPanelOpen = useStore((s) => s.filesPanelOpen);
  const isNarrow = useStore((s) => s.isNarrow);
  const showFiles = currentMode === "code" && filesPanelOpen && !isNarrow;

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true); // 用户是否贴在底部（贴底才自动跟随流式）

  // 订阅 store：流式 / 消息变化时若贴底则滚到底（不经 React render，省开销）。
  useEffect(() => {
    const scrollToBottom = () => {
      const el = scrollRef.current;
      if (el && stickRef.current) el.scrollTop = el.scrollHeight;
    };
    scrollToBottom();
    return store.subscribe(() => requestAnimationFrame(scrollToBottom));
  }, []);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  return (
    <div className={`conversation${showFiles ? " conversation--with-files" : ""}`}>
      <div className="conversation__main">
        <div className="conversation__scroll scroll-area" ref={scrollRef} onScroll={onScroll}>
          <MessageList />
        </div>
        <div className="conversation__composer">
          <div className="composer-col">
            <Composer />
          </div>
        </div>
      </div>
      {showFiles && <FilePane />}
    </div>
  );
}
