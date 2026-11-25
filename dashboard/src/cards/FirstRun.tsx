import { useState } from "react";
import type { components } from "../api";
import { PlateShell } from "./Plate";
import "./firstrun.css";

export type FirstRunProps = NonNullable<components["schemas"]["Overview"]["first_run"]>;

export function FirstRun({ firstRun }: { firstRun: FirstRunProps }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const workout = firstRun.workout;

  function copy() {
    const write = navigator.clipboard?.writeText(firstRun.message) ?? Promise.reject();
    write.then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      },
      () => setFailed(true),
    );
  }

  return (
    <PlateShell colour="orange">
      <div className="plate-forecast">
        <div className="plate-label">Your latest workout</div>
        {workout ? (
          <>
            {workout.minutes !== null ? (
              <div className="plate-hero">
                {Math.round(workout.minutes)}
                <span className="plate-unit">min</span>
              </div>
            ) : (
              <div className="plate-title first-run-type">{workout.type}</div>
            )}
            <div className="first-run-workout">
              {workout.type} ·{" "}
              {new Date(`${workout.date}T00:00`).toLocaleDateString(undefined, {
                weekday: "short",
                day: "numeric",
                month: "short",
              })}
              {workout.source && ` · ${workout.source}`}
            </div>
          </>
        ) : (
          <div className="plate-title">No workouts yet</div>
        )}
      </div>
      <div className="plate-today">
        <div className="plate-label">Next step</div>
        <div className="first-run-step">Set your first training goal in your AI tool</div>
        <button type="button" className="first-run-copy" onClick={copy}>
          {copied ? "Copied. Paste it into your AI tool." : "Copy first message"}
        </button>
        {failed && <div className="plate-label first-run-message">{firstRun.message}</div>}
      </div>
      {firstRun.waiting.length > 0 && (
        <div className="plate-road first-run-waiting">
          {firstRun.waiting.map((source) => (
            <div key={source.app}>
              Waiting for the first data from {source.app}.{source.phone && " Keep the TFS window open."}
            </div>
          ))}
        </div>
      )}
    </PlateShell>
  );
}
