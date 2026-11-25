import { Overview } from "./Overview";

export function Shell() {
  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="name">Training For Something</div>
        <a className="tab" href="/" aria-current="page">
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
            aria-hidden
          >
            <path d="M4 13h6V4H4zM14 20h6V11h-6zM14 8h6V4h-6zM4 20h6v-3H4z" />
          </svg>
          Overview
        </a>
      </nav>
      <main className="page">
        <Overview />
      </main>
    </div>
  );
}
