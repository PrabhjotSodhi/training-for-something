import { useState } from "react";
import type { components } from "../api";
import { LineChart } from "../charts/LineChart";
import "./trend.css";

export type TrendProps = NonNullable<components["schemas"]["TrendOverviewCard"]["data"]>;

const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 1 });
const percent = (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(Math.round(value))}%`;

export function Trend({ trend }: { trend: TrendProps }) {
  const [selected, setSelected] = useState(trend.selected);
  const part = trend.parts.find((candidate) => candidate.key === selected) ?? trend.parts[0];
  const isLoad = part.percent !== null;
  const format = (value: number) => (isLoad ? percent((value - 1) * 100) : number(value));

  return (
    <div className="trend">
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
      <LineChart
        points={part.points}
        start={part.points[0].date}
        end={part.points.at(-1)!.date}
        joinDays={trend.join_days}
        name={part.name}
        format={format}
        unit={isLoad ? undefined : (trend.unit ?? undefined)}
      />
    </div>
  );
}
