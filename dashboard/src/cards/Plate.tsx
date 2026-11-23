import { MeshGradient } from "@paper-design/shaders-react";
import type { components } from "../api";
import "./plate.css";

export type PlateProps = NonNullable<components["schemas"]["Overview"]["plate"]>;

const colours = {
  orange: ["#5A1E00", "#FF8A00", "#C2453A"],
  blue: ["#071F4A", "#0A7BFF", "#1E8FA0"],
  green: ["#0B3324", "#2FBF5A", "#2A8A80"],
  purple: ["#24093E", "#B04FE0", "#5257D6"],
};

const labels: Record<string, string> = {
  readiness: "Readiness",
  sleep_asleep: "Sleep",
  resting_hr: "Resting heart rate",
  hrv_sdnn: "HRV",
  load_cardio: "Cardio load",
  load_strength: "Strength load",
  load_effort: "Effort load",
};

const stillSetting = "(prefers-reduced-motion: reduce)";

export function Plate({ plate }: { plate: PlateProps }) {
  const data = plate.data;
  if (!data) return null;
  const [deep, goal, neighbour] = colours[plate.colour];
  const forecast = data.forecast;
  const progress = data.week && data.weeks ? (data.week / data.weeks) * 100 : 0;
  const date = new Date(`${data.date}T00:00`).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });

  return (
    <section className="plate" style={{ "--deep": deep } as React.CSSProperties}>
      <MeshGradient
        className="plate-field"
        colors={[deep, goal, deep, neighbour]}
        distortion={0.6}
        swirl={0.2}
        grainMixer={0.6}
        grainOverlay={0.5}
        speed={window.matchMedia(stillSetting).matches ? 0 : 0.1}
      />
      <div className="plate-shade" />
      <div className="plate-body">
        <div className="plate-forecast">
          <div className="plate-label">{forecast ? forecast.label : "Forecast"}</div>
          {forecast ? (
            <>
              <div className="plate-hero">
                {forecast.low}
                <span> to </span>
                {forecast.high}
                {forecast.unit && <span className="plate-unit">{forecast.unit}</span>}
              </div>
              <div className="plate-scale">
                <span
                  className="plate-fill"
                  style={{ left: `${forecast.start}%`, width: `${forecast.end - forecast.start}%` }}
                />
                {forecast.target !== null && (
                  <span className="plate-target" style={{ left: `${forecast.target}%` }} />
                )}
                {forecast.target !== null && (
                  <span className="plate-target-label" style={{ left: `${forecast.target}%` }}>
                    Goal {forecast.target_text}
                  </span>
                )}
              </div>
            </>
          ) : (
            <div className="plate-title">No forecast yet</div>
          )}
        </div>
        <div className="plate-today">
          <div className="plate-label">Today</div>
          <dl>
            {data.today.map((row) => (
              <div key={row.name}>
                <dt>{labels[row.name]}</dt>
                <dd>{row.value}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div className="plate-road">
          <div className="plate-road-top">
            <b>{data.goal}</b> {date}
          </div>
          <div className="plate-line">
            <span className="plate-line-fill" style={{ width: `${progress}%` }} />
            {data.week && <span className="plate-dot" style={{ left: `${progress}%` }} />}
            <span className="plate-end" />
          </div>
          <div className="plate-road-bottom">
            <span>{data.week && `Week ${data.week} of ${data.weeks}`}</span>
            <span>{data.days_to_go} days to go</span>
          </div>
        </div>
      </div>
    </section>
  );
}
