import { useState } from "react";
import { Bar, ComposedChart, Tooltip, XAxis, YAxis } from "recharts";
import type { components } from "../api";
import { ChartFrame, day, gutter, MARGIN } from "../charts/ChartFrame";
import { LineChart } from "../charts/LineChart";
import "./lift.css";
import "./trend.css";

type LiftData = components["schemas"]["LiftData"];
type Exercise = LiftData["exercises"][number];

const METRICS = ["Heaviest", "Volume"];
// A gap longer than two weeks breaks the line, so a long break shows as a break.
const JOIN_DAYS = 14;

const number = (kg: number) => kg.toLocaleString(undefined, { maximumFractionDigits: 1 });
const today = () => new Date().toLocaleDateString("en-CA");

function change(sessions: Exercise["sessions"]) {
  const [first, last] = [sessions[0], sessions.at(-1)!];
  if (sessions.length < 2) return `First logged ${day(first.date)}`;
  const kg = last.kg - first.kg;
  if (!kg) return `Same as ${day(first.date)}`;
  return `${kg > 0 ? "+" : "−"}${number(Math.abs(kg))} kg since ${day(first.date)}`;
}

function Volume({ weeks, kg }: { weeks: string[]; kg: (number | null)[] }) {
  const rows = weeks.map((date, index) => ({ index, date, kg: kg[index] }));
  const high = Math.max(...kg.map((value) => value ?? 0));
  const top = high * 1.15 || 1;
  return (
    <ChartFrame start={weeks[0]} labels={high ? gutter(high, 0, 0, top, number) : []}>
      <ComposedChart data={rows} margin={{ top: MARGIN, right: 0, bottom: MARGIN, left: 0 }}>
        <XAxis dataKey="index" type="number" domain={[-0.5, rows.length - 0.5]} hide />
        <YAxis domain={[0, top]} hide />
        <Tooltip
          isAnimationActive={false}
          cursor={{ fill: "var(--fill)" }}
          content={({ active, payload }) => {
            const row = payload?.[0]?.payload as (typeof rows)[number] | undefined;
            if (!active || row?.kg == null) return null;
            return (
              <div className="hover" role="tooltip">
                <b>Week of {day(row.date)}</b>
                <span>
                  Lifted<i>{number(row.kg)} kg</i>
                </span>
              </div>
            );
          }}
        />
        <Bar dataKey="kg" fill="var(--goal)" maxBarSize={18} radius={[4, 4, 0, 0]} isAnimationActive={false} />
      </ComposedChart>
    </ChartFrame>
  );
}

export function Lift({ lift }: { lift: LiftData }) {
  const [selected, setSelected] = useState(lift.selected);
  const [metric, setMetric] = useState(METRICS[0]);
  const exercise = lift.exercises.find((item) => item.id === selected)!;
  const volume = metric === "Volume";
  const lastSession = exercise.sessions.at(-1);
  const lastWeek = exercise.weekly_kg.map((kg) => kg != null).lastIndexOf(true);

  return (
    <div className="trend lift">
      <div className="trend-head">
        <div className="card-title">Lift progress</div>
      </div>
      <div className="lift-row">
        <label className="lift-pick">
          <select aria-label="Exercise" value={selected} onChange={(event) => setSelected(event.target.value)}>
            {lift.exercises.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <svg viewBox="0 0 12 12" aria-hidden="true">
            <path d="M3 4.5 6 7.5 9 4.5" />
          </svg>
        </label>
        <div className="trend-chips" role="tablist" aria-label="Lift metric">
          {METRICS.map((name) => (
            <button
              key={name}
              role="tab"
              aria-selected={metric === name}
              className="trend-chip"
              onClick={() => setMetric(name)}
            >
              {name}
            </button>
          ))}
        </div>
      </div>
      {volume ? (
        <>
          <div className="trend-state">
            {lastWeek < 0 ? "No volume in the last 12 weeks" : `Week of ${day(lift.weeks[lastWeek])}`}
          </div>
          {lastWeek >= 0 && (
            <div className="trend-value">
              {number(exercise.weekly_kg[lastWeek]!)}
              <span>kg lifted</span>
            </div>
          )}
          <Volume weeks={lift.weeks} kg={exercise.weekly_kg} />
        </>
      ) : (
        <>
          <div className="trend-state">
            {lastSession ? change(exercise.sessions) : "No workouts in the last 12 weeks"}
          </div>
          {lastSession && (
            <div className="trend-value">
              {number(lastSession.kg)}
              <span>kg on {day(lastSession.date)}</span>
            </div>
          )}
          <LineChart
            points={exercise.sessions.map((session) => ({ date: session.date, value: session.kg }))}
            start={lift.weeks[0]}
            end={today()}
            joinDays={JOIN_DAYS}
            name="Heaviest"
            format={number}
            unit="kg"
          />
        </>
      )}
    </div>
  );
}
