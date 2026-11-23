import { useState } from "react";
import { Area, ComposedChart, Line, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { components } from "../api";
import "./trend.css";

export type TrendProps = NonNullable<components["schemas"]["TrendOverviewCard"]["data"]>;
type Part = TrendProps["parts"][number];

const HEIGHT = 150;
const MARGIN = 12;
const DAY = 86_400_000;

const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 1 });
const percent = (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(Math.round(value))}%`;
const day = (date: string) =>
  new Date(`${date}T00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" });

export function Trend({ trend }: { trend: TrendProps }) {
  const [selected, setSelected] = useState(trend.selected);
  const part = trend.parts.find((candidate) => candidate.key === selected) ?? trend.parts[0];
  const isLoad = part.percent !== null;
  const show = (value: number, unit = "") =>
    isLoad ? percent((value - 1) * 100) : `${number(value)}${unit}`;

  const rows = part.points.map((point, index) => ({
    ...point,
    index,
    band: point.low === null || point.high === null ? null : [point.low, point.high],
  }));
  const readings = rows.filter((row) => row.value !== null);
  const segments: number[] = [];
  let group = -1;
  readings.forEach((row, position) => {
    const gap = position && (Date.parse(row.date) - Date.parse(readings[position - 1].date)) / DAY;
    if (!position || gap > trend.join_days) group += 1;
    segments[row.index] = group;
  });
  const groups = Array.from({ length: group + 1 }, (_, number) => number);
  const withSegments = rows.map((row) => {
    const own = segments[row.index];
    if (own === undefined) return row;
    const near = [rows[row.index - 1], row, rows[row.index + 1]].filter(
      (other) => other && segments[other.index] === own,
    );
    const outside = near.some(
      (other) => other.low !== null && (other.value! < other.low || other.value! > other.high!),
    );
    return { ...row, [`in${own}`]: row.value, [`out${own}`]: outside ? row.value : null };
  });

  const last = readings.at(-1)!;
  const banded = readings.filter((row) => row.low !== null).at(-1);
  const values = readings.flatMap((row) => [row.value!, ...(row.low === null ? [] : [row.low, row.high!])]);
  const span = Math.max(...values) - Math.min(...values) || 1;
  const bottom = Math.min(...values) - span * 0.35;
  const top = Math.max(...values) + span * 0.35;
  const at = (value: number) => MARGIN + ((top - value) / (top - bottom)) * (HEIGHT - MARGIN * 2);
  const middle = banded ? (at(banded.high!) + at(banded.low!)) / 2 : 0;
  const half = banded ? Math.max(8, (at(banded.low!) - at(banded.high!)) / 2) : 0;
  const hasDot = (index: number) =>
    trend.join_days > 1 ||
    (segments[index - 1] !== segments[index] && segments[index + 1] !== segments[index]);

  return (
    <div className="trend" data-colour={trend.colour}>
      <div className="trend-head">
        <div className="card-title">{trend.title}</div>
        {trend.parts.length > 1 && (
          <div className="trend-chips" role="tablist" aria-label="Load type">
            {trend.parts.map((candidate) => (
              <button
                key={candidate.key}
                role="tab"
                aria-selected={candidate.key === part.key}
                className="trend-chip"
                onClick={() => setSelected(candidate.key)}
              >
                {candidate.name}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="trend-state">{part.state}</div>
      <div className="trend-value">
        {isLoad ? percent(part.percent!) : number(part.value!)}
        <span>{isLoad ? "last 7 days vs your usual" : (trend.unit ?? "")}</span>
      </div>
      <div className="trend-plot" style={{ height: HEIGHT }}>
        <div className="trend-chart">
          <ResponsiveContainer width="100%" height={HEIGHT}>
            <ComposedChart data={withSegments} margin={{ top: MARGIN, right: 0, bottom: MARGIN, left: 0 }}>
              <XAxis dataKey="index" type="number" domain={[0, rows.length - 1]} hide />
              <YAxis domain={[bottom, top]} hide />
              <Area dataKey="band" type="monotone" stroke="none" fill="var(--band)" fillOpacity={1} isAnimationActive={false} activeDot={false} />
              <Tooltip
                isAnimationActive={false}
                cursor={{ stroke: "var(--muted)", strokeWidth: 1 }}
                content={({ active, payload }) => {
                  const row = payload?.[0]?.payload as (typeof rows)[number] | undefined;
                  if (!active || !row) return null;
                  return (
                    <div className="trend-hover" role="tooltip">
                      <b>{day(row.date)}</b>
                      <span>
                        {part.name}
                        <i>{row.value === null ? "No data" : show(row.value, ` ${trend.unit ?? ""}`.trimEnd())}</i>
                      </span>
                      {row.low !== null && (
                        <>
                          <span>
                            Low<i>{show(row.low)}</i>
                          </span>
                          <span>
                            High<i>{show(row.high!)}</i>
                          </span>
                        </>
                      )}
                    </div>
                  );
                }}
              />
              {groups.map((each) => (
                <Line
                  key={`in${each}`}
                  dataKey={`in${each}`}
                  type="monotone"
                  stroke="var(--ink)"
                  strokeWidth={2.25}
                  strokeLinecap="round"
                  connectNulls
                  isAnimationActive={false}
                  dot={(dot) =>
                    dot.value != null && hasDot(dot.index) ? (
                      <circle key={dot.index} cx={dot.cx} cy={dot.cy} r={3} className="trend-dot" />
                    ) : (
                      <g key={dot.index} />
                    )
                  }
                  activeDot={{ r: 4, fill: "var(--ink)", stroke: "var(--card)", strokeWidth: 2 }}
                />
              ))}
              {groups.map((each) => (
                <Line
                  key={`out${each}`}
                  dataKey={`out${each}`}
                  type="monotone"
                  stroke="var(--goal)"
                  strokeWidth={2.75}
                  strokeLinecap="round"
                  isAnimationActive={false}
                  dot={false}
                  activeDot={false}
                />
              ))}
              <ReferenceDot
                x={last.index}
                y={last.value!}
                ifOverflow="visible"
                shape={({ cx, cy }: { cx?: number; cy?: number }) => (
                  <g>
                    <circle cx={cx} cy={cy} r={11} className="trend-halo" />
                    <circle cx={cx} cy={cy} r={5.5} className="trend-today" />
                  </g>
                )}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
        <div className="trend-gutter">
          {banded && (
            <>
              <span style={{ top: middle - half }}>{show(banded.high!)}</span>
              <span style={{ top: middle + half }}>{show(banded.low!)}</span>
            </>
          )}
        </div>
      </div>
      <div className="trend-x">
        <span>{day(rows[0].date)}</span>
        <span>Today</span>
      </div>
    </div>
  );
}
