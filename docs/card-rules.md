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

## Goal plate

1. The plate sits above the cards, full width, at least 42 percent of the screen height, with a 20 px radius and no shadow.
2. The background is an animated mesh gradient from `@paper-design/shaders-react`, pinned to one exact version. With reduced motion on, the gradient stands still.
3. Each goal colour is a fixed set of colours: orange `#5A1E00 #FF8A00 #C2453A`, blue `#071F4A #0A7BFF #1E8FA0`, green `#0B3324 #2FBF5A #2A8A80`, purple `#24093E #B04FE0 #5257D6`. A shade layer of the darkest colour sits over the gradient so pale text passes 4.5:1.
4. Three groups sit on the gradient with no cards and no takeaway sentence. The forecast with its range and the goal mark is at the top left. The Today rows, in the order the layout lists them, are at the top right. The timeline runs along the bottom: goal name and date above the line, "Week N of M" and "N days to go" below it.
5. The forecast number is 96 px, 700 weight. All other text uses the type scale.
6. No forecast shows "No forecast yet". A row with no value today shows "No data". No plan items hides "Week N of M".
7. Week 1 is the week the plan starts. M is the weeks from the plan start to the goal date, rounded up.
