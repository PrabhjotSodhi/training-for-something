import type { components } from "../api";
import "./muscles.css";

export type MusclesProps = NonNullable<components["schemas"]["SetsByMuscleOverviewCard"]["data"]>;

export function Muscles({ muscles }: { muscles: MusclesProps }) {
  const total = muscles.muscles.reduce((sum, muscle) => sum + muscle.sets, 0);
  const most = Math.max(...muscles.muscles.map((muscle) => muscle.sets));
  return (
    <div className="muscles">
      <div className="card-title">Sets by muscle</div>
      <div className="muscles-headline">{total ? `${total} sets` : "No sets this week"}</div>
      <div className="muscles-rows">
        {muscles.muscles.map((muscle) => (
          <div key={muscle.name} className="muscles-row">
            <div className="muscles-name">{muscle.name.replaceAll("_", " ")}</div>
            <div className="muscles-track">
              <span className="muscles-bar" style={{ width: `${(muscle.sets / most) * 100}%` }} />
            </div>
            <div className="muscles-value">{muscle.sets}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
