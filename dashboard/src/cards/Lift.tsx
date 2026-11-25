import { useState } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  type DotItemDotProps,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { components } from "../api";
import "./lift.css";
import "./trend.css";

type LiftData = components["schemas"]["LiftData"];
const shortDate = (date: string) =>
  new Date(`${date}T00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const number = (kg: number) => kg.toLocaleString(undefined, { maximumFractionDigits: 2 });
const tick = { fill: "var(--muted)", fontSize: 12 };

function renderDot({ cx, cy, index, points, payload }: DotItemDotProps) {
  if (index !== points.length - 1 || payload.kg == null) {
    return <circle key={index} cx={cx} cy={cy} r={3} fill="var(--ink)" />;
  }

  return (
    <g key={index}>
      <circle cx={cx} cy={cy} r={5} fill="var(--goal)" stroke="var(--ink)" />
      <text
        x={cx}
        y={cy}
        dy={-12}
        textAnchor={index ? "end" : "middle"}
        fill="var(--ink)"
        fontSize={13}
        fontWeight={700}
      >
        {shortDate(payload.date)} · {number(payload.kg)} kg
      </text>
    </g>
  );
}

export function Lift({ lift }: { lift: LiftData }) {
  const [selected, setSelected] = useState(lift.selected);
  const [metric, setMetric] = useState("Heaviest");
  const exercise = lift.exercises.find((item) => item.id === selected)!;
  const weekly = metric === "Weekly volume";
  const unit = weekly ? "kg lifted" : "kg";
  const rows = weekly
    ? lift.weeks.map((date, index) => ({ date, kg: exercise.weekly_kg[index] }))
    : exercise.sessions;

  return (
    <div className="lift">
      <div className="trend-head">
        <div className="card-title">Lift progress</div>
        <select
          aria-label="Exercise"
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
        >
          {lift.exercises.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </div>
      <div className="lift-controls">
        <div className="trend-chips" role="group" aria-label="Lift metric">
          {["Heaviest", "Weekly volume"].map((name) => (
            <button
              key={name}
              type="button"
              className="trend-chip"
              aria-pressed={metric === name}
              onClick={() => setMetric(name)}
            >
              {name}
            </button>
          ))}
        </div>
        <span className="card-note">Last 12 weeks</span>
      </div>
      <div className="lift-unit card-note">{weekly ? "kg lifted" : "kg per workout"}</div>
      <ResponsiveContainer width="100%" height={166}>
        <ComposedChart
          data={rows}
          margin={{ top: 23, right: 12, bottom: 0, left: 0 }}
          aria-label={`${exercise.name}, ${metric}, last 12 weeks`}
        >
          <CartesianGrid stroke="var(--line)" vertical={false} />
          <XAxis
            dataKey="date"
            tickFormatter={shortDate}
            tick={tick}
            tickLine={false}
            axisLine={false}
            minTickGap={28}
            tickMargin={8}
          />
          <YAxis
            tick={tick}
            tickCount={3}
            width={42}
            axisLine={false}
            tickLine={false}
            domain={weekly ? [0, "auto"] : ["auto", "auto"]}
          />
          <Tooltip
            isAnimationActive={false}
            cursor={{ stroke: "var(--faint)", fill: "var(--fill)" }}
            content={({ active, payload }) => {
              const row = payload?.[0]?.payload;
              if (!active || row?.kg == null) return null;

              return (
                <div className="hover" role="tooltip">
                  <b>
                    {weekly && "Week of "}{shortDate(row.date)}
                  </b>
                  <span>
                    {metric}
                    <i>{number(row.kg)} {unit}</i>
                  </span>
                </div>
              );
            }}
          />
          {weekly ? (
            <Bar
              dataKey="kg"
              fill="var(--goal)"
              stroke="var(--goal-ink)"
              maxBarSize={28}
              radius={[4, 4, 0, 0]}
              isAnimationActive={false}
            />
          ) : (
            <Line
              dataKey="kg"
              stroke="var(--ink)"
              strokeWidth={2}
              isAnimationActive={false}
              dot={renderDot}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
      {!rows.some((row) => row.kg != null) && (
        <div className="card-note">
          No {weekly ? "volume" : "working weights"} in the last 12 weeks.
        </div>
      )}
    </div>
  );
}
