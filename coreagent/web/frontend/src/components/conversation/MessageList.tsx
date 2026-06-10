import { Message } from "./Message";
import { LiveMessage } from "./LiveMessage";
import { shallowEqual, useStore } from "../../state/store";

/** 消息线：权威存储消息 + 流式 live 气泡。messages 变化才重渲染（live 由 LiveMessage 自订阅）。 */
export function MessageList() {
  const messages = useStore((s) => s.messages, shallowEqual);
  const hasLive = useStore((s) => s.live !== null);

  return (
    <div className="message-list">
      <div className="message-list__col">
        {messages.map((m, i) => (
          <Message key={i} message={m} index={i} />
        ))}
        {hasLive && <LiveMessage />}
      </div>
    </div>
  );
}
