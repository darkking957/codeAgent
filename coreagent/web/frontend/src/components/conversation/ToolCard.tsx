import { Search, Wrench } from "lucide-react";

import type { LiveItem } from "../../state/store";

type Input = Record<string, unknown>;
const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

// —— code 模式工具入参视图（由 input 重建预览；后端不算 diff）——
function InputView({ name, input }: { name: string; input: Input }) {
  if (name === "edit_file") {
    return (
      <div className="tool-input">
        <div className="tool-input__path">{str(input.path)}</div>
        <pre className="tool-input__del">{str(input.old_string)}</pre>
        <pre className="tool-input__ins">{str(input.new_string)}</pre>
      </div>
    );
  }
  if (name === "write_file") {
    return (
      <div className="tool-input">
        <div className="tool-input__path">写入 {str(input.path)}</div>
        <pre className="tool-input__ins">{str(input.content)}</pre>
      </div>
    );
  }
  if (name === "run_command") {
    return <pre className="tool-input__cmd">$ {str(input.command)}</pre>;
  }
  return <pre className="tool-input__json">{JSON.stringify(input ?? {})}</pre>;
}

/** web_search 来源列表：标题 + 可点链接。 */
export function SearchSources({ sources }: { sources: { title?: string; url?: string }[] }) {
  return (
    <div className="sources">
      <div className="sources__head">来源：</div>
      {sources.map((s, i) => (
        <a
          key={i}
          className="sources__item"
          href={s.url || "#"}
          target="_blank"
          rel="noopener noreferrer"
        >
          {s.title || s.url || ""}
        </a>
      ))}
    </div>
  );
}

const headIcon = (name: string) =>
  name === "web_search" ? (
    <Search size={15} strokeWidth={1.75} />
  ) : (
    <Wrench size={15} strokeWidth={1.75} />
  );

/** 存储里的 tool_use / server_tool_use 块 → 工具调用卡（code 模式带入参视图）。 */
export function ToolUseCard({ name, input, mode }: { name: string; input: Input; mode: string }) {
  if (name === "web_search") {
    return (
      <div className="tool-card">
        <div className="tool-card__head">
          {headIcon(name)}
          <span className="tool-card__title">搜索：{str(input.query)}</span>
        </div>
      </div>
    );
  }
  return (
    <div className="tool-card">
      <div className="tool-card__head">
        {headIcon(name)}
        <span className="tool-card__title">{name}</span>
      </div>
      {mode === "code" && <InputView name={name} input={input} />}
    </div>
  );
}

/** 存储里承载 tool_result 的 user 消息块 → 结果卡。 */
export function ToolResultCard({ content, isError }: { content: unknown; isError: boolean }) {
  const text = typeof content === "string" ? content : JSON.stringify(content);
  return (
    <div className={`tool-card${isError ? " tool-card--err" : ""}`}>
      <div className={isError ? "status-bad" : "status-ok"}>{isError ? "✗ 失败" : "✓ 完成"}</div>
      <pre className="tool-output">{text}</pre>
    </div>
  );
}

/** 流式 live 工具卡（reference 无对应；#0023 行为）：调用入参 + 落定结果同卡。 */
export function LiveToolCard({ item, mode }: { item: Extract<LiveItem, { kind: "tool" }>; mode: string }) {
  const { name, input, result } = item;
  const isWebSearch = name === "web_search";
  return (
    <div className={`tool-card${result?.isError ? " tool-card--err" : ""}`}>
      <div className="tool-card__head">
        {headIcon(name)}
        <span className="tool-card__title">
          {isWebSearch ? `搜索：${str(input.query)}` : name}
        </span>
      </div>
      {mode === "code" && !isWebSearch && <InputView name={name} input={input} />}
      {result && (
        <div className="tool-card__result">
          <div className={result.isError ? "status-bad" : "status-ok"}>
            {result.isError ? (result.rejected ? "⛔ 被拒" : "✗ 失败") : "✓ 完成"}
          </div>
          {isWebSearch && !result.isError ? (
            <SearchSources sources={safeSources(result.content)} />
          ) : (
            <pre className={name === "run_command" ? "tool-output tool-output--term" : "tool-output"}>
              {result.content}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function safeSources(content: string): { title?: string; url?: string }[] {
  try {
    const parsed = JSON.parse(content || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}
