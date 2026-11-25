import "./barrows.css";

export type BarRow = { key: string; label: string; value: number | null; text: string };

type BarRowsProps = {
  rows: BarRow[];
  max: number;
  labelWidth: number;
  valueWidth: number;
  target?: { value: number; label: string } | null;
  highlight?: string;
  missing?: string;
};

export function BarRows({ rows, max, labelWidth, valueWidth, target, highlight, missing }: BarRowsProps) {
  const style = { "--label": `${labelWidth}px`, "--value": `${valueWidth}px` } as React.CSSProperties;
  return (
    <div className={target ? "bars with-target" : "bars"} style={style}>
      {target && (
        <div
          className="bars-target"
          style={{ left: `calc(var(--label) + (100% - var(--label) - var(--value)) * ${target.value / max})` }}
        >
          <span>{target.label}</span>
        </div>
      )}
      {rows.map((row) => (
        <div key={row.key} className={row.key === highlight ? "bars-row now" : "bars-row"}>
          <div className="bars-label">{row.label}</div>
          <div className="bars-track">
            {row.value === null ? (
              <span className="bars-missing">{missing}</span>
            ) : (
              <span className="bars-bar" style={{ width: `${(row.value / max) * 100}%` }} />
            )}
          </div>
          <div className="bars-value">{row.value === null ? "" : row.text}</div>
        </div>
      ))}
    </div>
  );
}
