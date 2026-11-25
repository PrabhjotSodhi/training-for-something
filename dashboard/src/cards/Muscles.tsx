import type { components } from "../api";
import { BarRows } from "./BarRows";

export type MusclesProps = NonNullable<components["schemas"]["SetsByMuscleOverviewCard"]["data"]>;

export function Muscles({ muscles }: { muscles: MusclesProps }) {
  const total = muscles.muscles.reduce((sum, muscle) => sum + muscle.sets, 0);
  return (
    <div className="muscles">
      <div className="card-title">Sets by muscle</div>
      <div className="card-headline">{total ? `${total} sets` : "No sets this week"}</div>
      <BarRows
        rows={muscles.muscles.map((muscle) => ({
          key: muscle.name,
          label: muscle.name.replaceAll("_", " "),
          value: muscle.sets,
          text: String(muscle.sets),
        }))}
        max={Math.max(...muscles.muscles.map((muscle) => muscle.sets))}
        labelWidth={96}
        valueWidth={32}
      />
    </div>
  );
}
