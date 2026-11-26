import { useEffect, useState } from "react";
import type { components } from "./api";
import { catalogue } from "./catalogue";
import { FirstRun } from "./cards/FirstRun";
import { Plate } from "./cards/Plate";

type Overview = components["schemas"]["Overview"];

function SettingsLink() {
  return (
    <a className="round-link settings-link" href="#settings" aria-label="Settings">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
        <path strokeLinejoin="round" d="m9.5 3 .6-1h3.8l.6 2.3 2 .9 2.1-.7 1.9 3.3-1.6 1.7.2 2.2 1.6 1.6-1.9 3.4-2.2-.6-1.9 1-.6 2.3h-3.8l-.7-2.3-1.9-1-2.2.6-1.9-3.4 1.6-1.6.1-2.2L3.8 7.8l1.9-3.3 2.1.7 1.6-.8Z" />
        <circle cx="12" cy="10.8" r="3.1" />
      </svg>
    </a>
  );
}

export function Overview() {
  const [overview, setOverview] = useState<Overview>();
  const [reason, setReason] = useState<string>();

  useEffect(() => {
    fetch("/api/overview")
      .then((response) => {
        if (!response.ok) throw new Error(`TFS answered ${response.status}`);
        return response.json();
      })
      .then(
        (overview: Overview) => {
          if (overview.plate) document.documentElement.dataset.goal = overview.plate.colour;
          setOverview(overview);
        },
        (error: Error) => setReason(error.message),
      );
  }, []);

  if (reason) return <div className="card with-settings"><SettingsLink /><p className="error">The page cannot reach TFS: {reason}</p></div>;

  if (!overview)
    return (
      <div className="cards">
        <div className="card full with-settings"><SettingsLink /></div>
        <div className="card" />
        <div className="card" />
      </div>
    );

  const hasPlate = Boolean(overview.plate?.data || overview.first_run);
  const firstCard = overview.cards.findIndex(card => catalogue[card.type]);
  return (
    <>
      {overview.new_version && <NewVersion version={overview.new_version} />}
      {hasPlate && <div className="overview-plate">
        <SettingsLink />
        {overview.plate && <Plate plate={overview.plate} />}
        {overview.first_run && <FirstRun firstRun={overview.first_run} />}
      </div>}
      <div className="cards">
        {!hasPlate && firstCard === -1 && <div className="card full with-settings"><SettingsLink /></div>}
        {overview.cards.map((card, index) => {
          const entry = catalogue[card.type];
          if (!entry) {
            console.warn(`Overview skips the unknown card type "${card.type}"`);
            return null;
          }
          const withSettings = !hasPlate && index === firstCard;
          return (
            <div key={index} className={`card card-${card.type}${entry.full ? " full" : ""}${withSettings ? " with-settings" : ""}`}>
              {withSettings && <SettingsLink />}
              {entry.draw(card)}
            </div>
          );
        })}
      </div>
    </>
  );
}

function NewVersion({ version }: { version: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <p className="new-version">
      <span>
        TFS {version} is ready. Run <code>tfs update</code>.
      </span>
      <button
        type="button"
        aria-label="Copy tfs update"
        onClick={() => navigator.clipboard.writeText("tfs update").then(() => setCopied(true))}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </p>
  );
}
