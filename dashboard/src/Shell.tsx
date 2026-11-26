import { useEffect, useState } from "react";
import { Overview } from "./Overview";
import { Settings } from "./Settings";

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

export function Shell() {
  const [hash, setHash] = useState(location.hash);
  const settings = hash === "#settings";
  useEffect(() => {
    const navigate = () => {
      setHash(location.hash);
      window.scrollTo(0, 0);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && location.hash === "#settings") location.hash = "";
    };
    window.addEventListener("hashchange", navigate);
    window.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("hashchange", navigate);
      window.removeEventListener("keydown", escape);
    };
  }, []);
  return (
    <main className="page">
      {!settings && <SettingsLink />}
      {settings ? <Settings /> : <Overview />}
    </main>
  );
}
