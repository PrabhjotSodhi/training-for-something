import type { components } from "../api";
import { BarRows } from "./BarRows";
import "./sleep.css";

export type SleepProps = NonNullable<components["schemas"]["SleepOverviewCard"]["data"]>;
type Night = SleepProps["nights"][number];

const STAGES = ["deep", "core", "rem", "awake"];
const HOVER_STAGES = [
  ["deep", "Deep"],
  ["rem", "REM"],
  ["core", "Core"],
];

function duration(hours: number, short = false) {
  const total = Math.round(hours * 60);
  const [h, m] = [Math.floor(total / 60), total % 60];
  if (!h) return `${m} min`;
  if (!m) return `${h} h`;
  return short ? `${h} h ${String(m).padStart(2, "0")}` : `${h} h ${m} min`;
}

const parts = (night: Night) =>
  STAGES.flatMap((name) => (night.stages[name] == null ? [] : [{ name, value: night.stages[name] }]));

function NightHover({ night }: { night: Night }) {
  return (
    <>
      <b>
        {new Date(`${night.date}T00:00`).toLocaleDateString(undefined, {
          weekday: "short",
          day: "numeric",
          month: "short",
        })}
      </b>
      <span>
        Asleep<i>{duration(night.hours!, true)}</i>
      </span>
      {HOVER_STAGES.map(([name, label]) => (
        <span key={name}>
          <span className="sleep-key">
            <em className={`part-${name}`} />
            {label}
          </span>
          <i>{night.stages[name] == null ? "No data" : duration(night.stages[name], true)}</i>
        </span>
      ))}
    </>
  );
}

export function Sleep({ sleep }: { sleep: SleepProps }) {
  const slept = sleep.nights.flatMap((night) => (night.hours === null ? [] : [night.hours]));
  const average = slept.length ? slept.reduce((sum, hours) => sum + hours, 0) / slept.length : null;
  const inBed = sleep.nights.map((night) => parts(night).reduce((sum, part) => sum + part.value, 0));
  const scale = Math.max(9, sleep.target_h ?? 0, ...slept, ...inBed);
  return (
    <div className="sleep">
      <div className="card-title">Sleep</div>
      <div className="card-headline">
        {average === null ? "No data" : `${duration(average)} a night`}
      </div>
      <BarRows
        rows={sleep.nights.map((night) => ({
          key: night.date,
          label: new Date(`${night.date}T00:00`).toLocaleDateString(undefined, { weekday: "short" }),
          value: night.hours,
          text: night.hours === null ? "" : duration(night.hours, true),
          parts: parts(night),
          hover: parts(night).length ? <NightHover night={night} /> : undefined,
        }))}
        max={scale}
        labelWidth={36}
        valueWidth={64}
        target={sleep.target_h === null ? null : { value: sleep.target_h, label: `${duration(sleep.target_h)} goal` }}
        highlight={sleep.nights[6].date}
        missing="No data"
      />
    </div>
  );
}
