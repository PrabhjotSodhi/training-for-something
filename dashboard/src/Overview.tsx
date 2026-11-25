import { useEffect, useState } from "react";
import type { components } from "./api";
import { catalogue } from "./catalogue";
import { FirstRun } from "./cards/FirstRun";
import { Plate } from "./cards/Plate";

type Overview = components["schemas"]["Overview"];

export function Overview() {
  const [overview, setOverview] = useState<Overview>();
  const [reason, setReason] = useState<string>();

  useEffect(() => {
    fetch("/api/overview")
      .then((response) => {
        if (!response.ok) throw new Error(`TFS answered ${response.status}`);
        return response.json();
      })
      .then(setOverview, (error: Error) => setReason(error.message));
  }, []);

  if (reason) return <p className="error">The page cannot reach TFS: {reason}</p>;

  if (!overview)
    return (
      <div className="cards">
        <div className="card full" />
        <div className="card" />
        <div className="card" />
      </div>
    );

  return (
    <>
      {overview.plate && <Plate plate={overview.plate} />}
      {overview.first_run && <FirstRun firstRun={overview.first_run} />}
      <div className="cards">
        {overview.cards.map((card, index) => {
          const entry = catalogue[card.type];
          if (!entry) {
            console.warn(`Overview skips the unknown card type "${card.type}"`);
            return null;
          }
          return (
            <div key={index} className={entry.full ? "card full" : "card"}>
              {entry.draw(card)}
            </div>
          );
        })}
      </div>
    </>
  );
}
