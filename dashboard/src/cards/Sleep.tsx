import type { components } from "../api";
import { BarRows } from "./BarRows";

export type SleepProps = NonNullable<components["schemas"]["SleepOverviewCard"]["data"]>;

function duration(hours: number, short = false) {
  const total = Math.round(hours * 60);
  const [h, m] = [Math.floor(total / 60), total % 60];
  if (!h) return `${m} min`;
  if (!m) return `${h} h`;
  return short ? `${h} h ${String(m).padStart(2, "0")}` : `${h} h ${m} min`;
}

export function Sleep({ sleep }: { sleep: SleepProps }) {
  const slept = sleep.nights.flatMap((night) => (night.hours === null ? [] : [night.hours]));
  const average = slept.length ? slept.reduce((sum, hours) => sum + hours, 0) / slept.length : null;
  const scale = Math.max(9, sleep.target_h ?? 0, ...slept);
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
        }))}
        max={scale}
        labelWidth={36}
        valueWidth={64}
        target={sleep.target_h === null ? null : { value: sleep.target_h, label: duration(sleep.target_h) }}
        highlight={sleep.nights[6].date}
        missing="No data"
      />
    </div>
  );
}
