# Card rules

The rules for the dashboard shell and every card on it. Each card ticket adds its own rules here.

## Type

Plus Jakarta Sans for everything, with tabular figures on numbers. The font files ship inside the package under `dashboard/src/fonts/`, with their licence (SIL Open Font License). The page makes no request to any outside server.

| Style | Size / line height | Weight | Tracking |
|---|---|---|---|
| number | 34 / 0.95 | 700 | -4% |
| title | 19 / 1.1 | 700 | -1% |
| body | 15 / 21 | 400 | -0.5% |
| caption | 12 / 16 | 400 | 0 |

## Shell

1. The sidebar sits on the left. It shows the name "Training For Something" in text, then the tabs.
2. Only the Overview tab shows.
3. The page follows the system setting for light or dark.
4. Desktop width comes first.

## Colour

| Part | Light | Dark |
|---|---|---|
| Page | `#f5f5f7` | `#0a0a0b` |
| Sidebar | `#ffffff` | `#1c1c1e` |
| Text | `#1c1c1e` | `#f5f5f7` |
| Muted text | `#6b6b70` | `#98989f` |
| Lines | `#e5e5ea` | `#2c2c2e` |

## Surfaces

1. Two surfaces only: the page and the card.
2. No shadows on any surface.
3. A card has the card fill (`#ffffff` in light, `#1c1c1e` in dark), a 1 px line edge, a 20 px radius and 24 px padding.

## Grid

1. Cards sit in two columns with a 12 px gap. The week card spans both columns.
2. Cards in one row share one height.
3. While the page loads, empty cards keep their size so nothing jumps.
4. A failed load shows one muted line that says the page cannot reach TFS and why.
5. A card type the page does not know is skipped and logged in the console.
