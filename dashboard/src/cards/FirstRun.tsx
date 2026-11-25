import { useState } from "react";
import type { components } from "../api";
import "./firstrun.css";

export type FirstRunProps = NonNullable<components["schemas"]["Overview"]["first_run"]>;

export function FirstRun({ firstRun }: { firstRun: FirstRunProps }) {
  const [copied, setCopied] = useState(false);
  const workout = firstRun.workout;

  function copy() {
    navigator.clipboard.writeText(firstRun.message).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <section className="card first-run">
      <div className="card-title">Your latest workout</div>
      {workout ? (
        <>
          <div className="card-headline">
            {workout.type}
            {workout.minutes !== null && `, ${Math.round(workout.minutes)} min`}
          </div>
          <div className="card-note">
            {new Date(`${workout.date}T00:00`).toLocaleDateString(undefined, {
              day: "numeric",
              month: "short",
              year: "numeric",
            })}
            {workout.source && ` from ${workout.source}`}
          </div>
        </>
      ) : (
        <div className="card-headline">No workouts yet.</div>
      )}
      {firstRun.waiting.map((source) => (
        <div key={source.app} className="card-note">
          Waiting for the first data from {source.app}.{source.phone && " Keep the TFS window open."}
        </div>
      ))}
      <div className="first-run-step">
        <div className="card-title">Set your first training goal in your AI tool</div>
        <button type="button" onClick={copy}>
          {copied ? "Copied" : "Copy first message"}
        </button>
      </div>
    </section>
  );
}
