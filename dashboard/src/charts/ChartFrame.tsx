import type { ReactElement } from "react";
import { ResponsiveContainer } from "recharts";
import "./chart.css";

export const HEIGHT = 150;
export const MARGIN = 12;

export const day = (date: string) =>
  new Date(`${date}T00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" });

/** Two gutter labels at their values' heights, kept at least 16 px apart so they never overlap. */
export function gutter(high: number, low: number, bottom: number, top: number, format: (value: number) => string) {
  const at = (value: number) => MARGIN + ((top - value) / (top - bottom)) * (HEIGHT - MARGIN * 2);
  const middle = (at(high) + at(low)) / 2;
  const half = Math.max(8, (at(low) - at(high)) / 2);
  return [
    { text: format(high), top: middle - half },
    { text: format(low), top: middle + half },
  ];
}

type ChartFrameProps = {
  start: string;
  labels: { text: string; top: number }[];
  children: ReactElement;
};

/** The plot, its right gutter labels and the row that runs from the first date to Today. */
export function ChartFrame({ start, labels, children }: ChartFrameProps) {
  return (
    <>
      <div className="chart-plot" style={{ height: HEIGHT }}>
        <div className="chart-area">
          <ResponsiveContainer width="100%" height={HEIGHT}>
            {children}
          </ResponsiveContainer>
        </div>
        <div className="chart-gutter">
          {labels.map((label) => (
            <span key={label.text + label.top} style={{ top: label.top }}>
              {label.text}
            </span>
          ))}
        </div>
      </div>
      <div className="chart-x">
        <span>{day(start)}</span>
        <span>Today</span>
      </div>
    </>
  );
}
