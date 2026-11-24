import { createElement, type ReactNode } from "react";
import type { components } from "./api";
import { Muscles } from "./cards/Muscles";
import { Sleep } from "./cards/Sleep";
import { Trend } from "./cards/Trend";
import { Week } from "./cards/Week";

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
  week: {
    full: true,
    draw: (card) =>
      "data" in card && card.data && "days" in card.data
        ? createElement(Week, { week: card.data })
        : placeholder(card),
  },
  sleep: {
    full: false,
    draw: (card) =>
      "data" in card && card.data && "nights" in card.data
        ? createElement(Sleep, { sleep: card.data })
        : placeholder(card),
  },
  trend: {
    full: false,
    draw: (card) =>
      "data" in card && card.data && "parts" in card.data
        ? createElement(Trend, { trend: card.data })
        : placeholder(card),
  },
  sets_by_muscle: {
    full: false,
    draw: (card) =>
      "data" in card && card.data && "muscles" in card.data
        ? createElement(Muscles, { muscles: card.data })
        : placeholder(card),
  },
};
