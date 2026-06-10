// Claude 风火花/星标（赤陶 --accent，唯一彩色点）。用 SVG 12 辐瓣放射状，crisp 于任意尺寸。
// reference §6.7：星标用 SVG（非 lucide）。

export function StarLogo({ size = 28, className }: { size?: number; className?: string }) {
  const spokes = Array.from({ length: 12 }, (_, i) => i * 30);
  return (
    <svg
      width={size}
      height={size}
      viewBox="-50 -50 100 100"
      className={className}
      style={{ color: "var(--accent)", display: "block" }}
      aria-hidden="true"
      focusable="false"
    >
      {spokes.map((deg) => (
        <line
          key={deg}
          x1="0"
          y1="-14"
          x2="0"
          y2="-42"
          stroke="currentColor"
          strokeWidth="7"
          strokeLinecap="round"
          transform={`rotate(${deg})`}
        />
      ))}
    </svg>
  );
}
