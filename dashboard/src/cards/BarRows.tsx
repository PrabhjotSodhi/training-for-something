import { useState, type ReactNode } from "react";
import "./barrows.css";

export type BarRow = {
  key: string;
  label: string;
  value: number | null;
  text: string;
  parts?: { name: string; value: number }[];
  hover?: ReactNode;
};

type BarRowsProps = {
  rows: BarRow[];
  max: number;
  labelWidth: number;
  valueWidth: number;
  target?: { value: number; label: string } | null;
  highlight?: string;
  missing?: string;
};

const width = (value: number, max: number) => `${(value / max) * 100}%`;

function Bar({ row, max }: { row: BarRow; max: number }) {
  if (!row.parts?.length) {
    return <span className="bars-bar" style={{ width: width(row.value!, max) }} />;
  }
  const total = row.parts.reduce((sum, part) => sum + part.value, 0);
  return (
    <span className="bars-stack" style={{ width: width(total, max) }}>
      {row.parts.map((part) => (
        <span key={part.name} className={`bars-part part-${part.name}`} style={{ flex: part.value }} />
      ))}
    </span>
  );
}

export function BarRows({ rows, max, labelWidth, valueWidth, target, highlight, missing }: BarRowsProps) {
  const [open, setOpen] = useState<string | null>(null);
  const style = { "--label": `${labelWidth}px`, "--value": `${valueWidth}px` } as React.CSSProperties;
  const className = ["bars", target && "with-target", open && "hovering"].filter(Boolean).join(" ");
  return (
    <div className={className} style={style} onMouseLeave={() => setOpen(null)}>
      {target && (
        <div
          className="bars-target"
          style={{ left: `calc(var(--label) + (100% - var(--label) - var(--value)) * ${target.value / max})` }}
        >
          <span>{target.label}</span>
        </div>
      )}
      {rows.map((row) => {
        const classes = ["bars-row", row.key === highlight && "now", row.key === open && "open"];
        const show = row.hover ? () => setOpen(row.key) : undefined;
        return (
          <div
            key={row.key}
            className={classes.filter(Boolean).join(" ")}
            tabIndex={row.hover ? 0 : undefined}
            onMouseEnter={show}
            onFocus={show}
            onBlur={() => setOpen(null)}
          >
            <div className="bars-label">{row.label}</div>
            <div className="bars-track">
              {row.value === null ? <span className="bars-missing">{missing}</span> : <Bar row={row} max={max} />}
            </div>
            <div className="bars-value">{row.value === null ? "" : row.text}</div>
            {row.key === open && (
              <div className="hover bars-hover" role="tooltip">
                {row.hover}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
