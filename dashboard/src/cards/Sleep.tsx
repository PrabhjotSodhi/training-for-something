import type { components } from "../api";
import "./sleep.css";

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
      <div className="sleep-headline">
        {average === null ? "No data" : `${duration(average)} a night`}
      </div>
      <div className="sleep-nights">
        {sleep.target_h !== null && (
          <div
            className="sleep-target"
            style={{ left: `calc(36px + (100% - 100px) * ${sleep.target_h / scale})` }}
          >
            <span>{duration(sleep.target_h)}</span>
          </div>
        )}
        {sleep.nights.map((night, index) => (
          <div key={night.date} className={index === 6 ? "sleep-night now" : "sleep-night"}>
            <div className="sleep-day">
              {new Date(`${night.date}T00:00`).toLocaleDateString(undefined, { weekday: "short" })}
            </div>
            <div className="sleep-track">
              {night.hours === null ? (
                <span className="sleep-missing">No data</span>
              ) : (
                <span className="sleep-bar" style={{ width: `${(night.hours / scale) * 100}%` }} />
              )}
            </div>
            <div className="sleep-value">{night.hours === null ? "" : duration(night.hours, true)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
