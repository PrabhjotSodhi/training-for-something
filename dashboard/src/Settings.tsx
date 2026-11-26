import { useEffect, useState, type ReactNode } from "react";
import type { components } from "./api";
// Simple Icons, CC0
import claude from "./assets/logos/claude.svg";
import cursor from "./assets/logos/cursor.svg";
import hevy from "./assets/logos/hevy.svg";
import meta from "./assets/logos/meta.svg";
import openai from "./assets/logos/openai.svg";
import opencode from "./assets/logos/opencode.svg";

// Each project's own icon
import antigravity from "./assets/logos/antigravity.svg";
import garmin from "./assets/logos/garmin-delta.svg";
import grok from "./assets/logos/grok.svg";
import healthAutoExport from "./assets/logos/healthautoexport.png";
import kiro from "./assets/logos/kiro.svg";
import pi from "./assets/logos/pi.svg";
import "./settings.css";

type Connections = components["schemas"]["Settings"];
type Appearance = "system" | "light" | "dark";
declare global {
  interface Window { setAppearance(choice: Appearance): void }
}

const setupNote = "To change these, run tfs setup on this computer.";

function Group({ title, children, note }: { title: string; children: ReactNode; note?: string }) {
  return (
    <section className={`settings-group group-${title.split(" ")[0].toLowerCase()}`}>
      <h2 className="card-title">{title}</h2>
      <div className="card settings-rows">{children}</div>
      {note && <p className="card-note">{note}</p>}
    </section>
  );
}

const logos: Record<string, [string, string]> = {
  "Claude Code": [claude, "claude"],
  Codex: [openai, "black"],
  Cursor: [cursor, "black"],
  OpenCode: [opencode, "black"],
  "Meta Muse Code": [meta, "meta"],
  Garmin: [garmin, "black"],
  Hevy: [hevy, "black"],
  "Google Antigravity CLI": [antigravity, "white"],
  "Grok Build": [grok, "black grok"],
  "Health Auto Export": [healthAutoExport, "white health-export"],
  "Kiro CLI": [kiro, "kiro"],
  Pi: [pi, "pi"],
};

function Logo({ label }: { label: string }) {
  if (label === "Apple Health export") {
    return <span className="logo-tile heart" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 21C9 18 2 13 2 8a5.5 5.5 0 0 1 10-3 5.5 5.5 0 0 1 10 3c0 5-7 10-10 13Z" /></svg>
    </span>;
  }
  const logo = logos[label];
  if (!logo) return null;
  return <span className={`logo-tile ${logo[1]}`} aria-hidden="true">
    <img src={logo[0]} alt="" aria-hidden="true" />
  </span>;
}

function Row({ label, tile, children }: { label: string; tile?: ReactNode; children: ReactNode }) {
  return <div className="settings-row"><span className="row-label">{tile}{label}</span><span>{children}</span></div>;
}

function PhoneRow({ phone }: { phone: components["schemas"]["PairedPhone"] }) {
  const [device, os] = phone.device?.split(" · ") ?? [];
  const android = device?.startsWith("Android ");
  const kind = android ? "Android" : device ?? "Phone";
  const date = phone.paired_at && new Date(phone.paired_at).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  const value = [android ? device : os, date && `Paired ${date}`].filter(Boolean).join(" · ");
  const tile = <span className="logo-tile" aria-hidden="true">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
      <rect x="6" y="2" width="12" height="20" rx="3" /><path d="M10 18.5h4" />
    </svg>
  </span>;
  return <Row label={kind} tile={tile}>{value || "Paired"}</Row>;
}

function StatusDot({ active }: { active: boolean }) {
  return <span className={active ? "status-dot active" : "status-dot"} aria-hidden="true" />;
}

function arrival(timestamp: string) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(timestamp)) / 60000));
  if (minutes < 1) return "Last data just now";
  if (minutes < 60) return `Last data ${minutes} min ago`;
  if (minutes < 1440) return `Last data ${Math.floor(minutes / 60)} h ago`;
  const days = Math.floor(minutes / 1440);
  return `Last data ${days} ${days === 1 ? "day" : "days"} ago`;
}

export function Settings() {
  const [appearance, setChoice] = useState(document.documentElement.dataset.appearance);
  const [data, setData] = useState<Connections>();
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      for (const url of ["/api/settings?check_tools=false", "/api/settings"]) {
        const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error(`TFS answered ${response.status}`);
        setData(await response.json());
      }
    }
    load().catch((reason: Error) => {
      if (!controller.signal.aborted) setError(reason.message);
    });
    return () => controller.abort();
  }, []);

  return (
    <div className="settings">
      <header className="settings-title">
        <a className="round-link" href="#" aria-label="Back to Overview">
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m15 5-7 7 7 7" />
          </svg>
        </a>
        <h1>Settings</h1>
      </header>
      <div className="settings-groups">
        <Group title="Appearance">
          <Row label="Theme">
            <span className="appearance-control" role="group" aria-label="Theme">
              {(["system", "light", "dark"] as const).map(choice =>
                <button key={choice} aria-pressed={choice === appearance} onClick={() => {
                  window.setAppearance(choice);
                  setChoice(choice);
                }}>{choice[0].toUpperCase() + choice.slice(1)}</button>
              )}
            </span>
          </Row>
        </Group>
        {error && <p className="error" role="alert">The page cannot reach TFS: {error}</p>}
        {data && <>
          <Group title="Apps" note={setupNote}>
            {data.apps.map(app => <Row key={app.label} label={app.label} tile={<Logo label={app.label} />}>
              <StatusDot active={app.connected && !!app.last_received_at} />
              {app.connected ? app.last_received_at ? arrival(app.last_received_at) : "Waiting for first data" : "Not connected"}
            </Row>)}
          </Group>
          <Group title="AI tools" note={setupNote}>
            {data.tools.map(tool => <Row key={tool.label} label={tool.label} tile={<Logo label={tool.label} />}>
              <StatusDot active={tool.status === "Connected"} />
              {tool.status ?? (error ? "Check failed" : "Checking…")}
            </Row>)}
          </Group>
          <Group title="Phones" note={setupNote + " To remove all phones, run tfs phones forget."}>
            {data.phones.length
              ? data.phones.map((phone, index) => <PhoneRow key={index} phone={phone} />)
              : <Row label="Home Wi-Fi">None</Row>}
          </Group>
          <Group title="About"><Row label="Version">TFS {data.version}</Row></Group>
        </>}
      </div>
    </div>
  );
}
