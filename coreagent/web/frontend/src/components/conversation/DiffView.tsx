// 真实快照统一 diff 渲染（#0023 file_diff 帧）：按行首着色（+ 增 / - 删 / @@ 块头）。

function lineClass(line: string): string {
  if (line.startsWith("+") && !line.startsWith("+++")) return "diff-line diff-line--ins";
  if (line.startsWith("-") && !line.startsWith("---")) return "diff-line diff-line--del";
  if (line.startsWith("@@")) return "diff-line diff-line--hunk";
  return "diff-line";
}

export function DiffView({ path, diff }: { path: string; diff: string }) {
  const lines = (diff || "").split("\n");
  return (
    <div className="tool-card">
      <div className="tool-card__head">
        <span className="tool-card__title">📝 变更 {path}</span>
      </div>
      <pre className="diff">
        {lines.map((line, i) => (
          <span key={i} className={lineClass(line)}>
            {line || " "}
          </span>
        ))}
      </pre>
    </div>
  );
}
