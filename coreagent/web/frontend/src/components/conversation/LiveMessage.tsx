import { LiveToolCard } from "./ToolCard";
import { DiffView } from "./DiffView";
import { useStore } from "../../state/store";

/** 流式 live 渲染（#0026 T6）：一轮 assistant 的有序条目（纯文本增量 + 工具卡 + diff）。
 * 落定后由收尾拉权威历史替换为后端 HTML（含代码高亮）。 */
export function LiveMessage() {
  const live = useStore((s) => s.live);
  const mode = useStore((s) => s.currentMode);
  if (!live) return null;

  const showTyping = live.items.length === 0;

  return (
    <div className="msg msg--asst msg--live">
      <div className="msg--asst__body">
        {showTyping && <div className="typing-dots" aria-label="生成中"><span /><span /><span /></div>}
        {live.items.map((it, i) => {
          switch (it.kind) {
            case "text":
              return (
                <div key={i} className="bubble raw">
                  {it.text}
                </div>
              );
            case "thinking":
              return (
                <div key={i} className="thinking">
                  {it.text}
                </div>
              );
            case "tool":
              return <LiveToolCard key={i} item={it} mode={mode} />;
            case "diff":
              return <DiffView key={i} path={it.path} diff={it.diff} />;
            case "retry":
              return (
                <div key={i} className="thinking">
                  （第 {it.attempt} 次重试，{it.wait}s 后…）
                </div>
              );
            case "error":
              return (
                <div key={i} className="tool-card tool-card--err">
                  <div className="tool-card__head">
                    <span className="tool-card__title">⚠ 运行出错：{it.errorType}</span>
                  </div>
                </div>
              );
            default:
              return null;
          }
        })}
      </div>
    </div>
  );
}
