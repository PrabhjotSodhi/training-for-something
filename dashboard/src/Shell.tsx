import { useEffect, useState } from "react";
import { Overview } from "./Overview";
import { Settings } from "./Settings";

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
      {settings ? <Settings /> : <Overview />}
    </main>
  );
}
