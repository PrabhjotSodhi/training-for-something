import { createElement, type ReactNode } from "react";
import type { components } from "./api";

export type Card = components["schemas"]["Overview"]["cards"][number];

function placeholder(card: Card): ReactNode {
  return createElement(
    "div",
    null,
    createElement("div", { className: "card-title" }, card.type),
    "metric" in card && createElement("div", { className: "card-note" }, card.metric),
  );
}

export const catalogue: Record<string, { full: boolean; draw: (card: Card) => ReactNode }> = {
  week: { full: true, draw: placeholder },
  sleep: { full: false, draw: placeholder },
  trend: { full: false, draw: placeholder },
};
