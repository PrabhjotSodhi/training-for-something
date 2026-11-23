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

## This week card

1. The card spans both columns. It shows Monday to Sunday of the current week, one column per day, today's column marked.
2. Each session is one tile. Its height follows its time. The tile shows the session name and its amount with the sport's unit: km for run and ride, m for swim, sets for strength. Time shows only in the hover, which also opens on keyboard focus.
3. A sport with no unit, or a session with no distance or sets, shows minutes.
4. A done session counts its own distance, or every set it holds. A workout matched to a plan item shows once, on that item's tile. A workout with no plan item is its own done tile.
5. A planned session in the past stays planned.
6. A day with no session says "Rest". The totals row shows done of planned per sport, each with a bar.

## Sleep card

1. The headline is the average hours asleep over the last 7 nights, like "7 h 35 min a night". With no night in the 7 it says "No data".
2. One row per night, oldest first, ending today: weekday, a bar and the time. A night belongs to the day the person wakes up.
3. A night with no data says "No data", has no bar and stays out of the average.
4. A dashed line marks the saved sleep goal in hours. With no saved goal there is no line.

## Trend card

1. One component draws resting heart rate, HRV, weight, VO2 max and training load: the value, a line over the shaded normal band, a dot on the latest reading, and the band edges as the only y labels. The stretch of line outside the band uses the goal colour from the layout's plate, orange with no plate.
2. The chart shows the 30 days ending today. A day with no reading is empty. Readings are joined by a line when they are 1 day apart for resting heart rate and HRV, and up to 14 days apart for weight and VO2 max. Weight and VO2 max also show a dot on each reading.
3. The band is the readiness rule: the median of the previous 42 days from the same source, plus and minus the spread, with at least 21 of those days. A day with no band draws no band and the line still shows.
4. Training load never shows load points. The line is the last 7 days over the usual week, with the band at 0.88 to 1.1. The value reads like "+16% last 7 days vs your usual". State words are Below normal, Normal and Above normal.
5. Training load has one part each for Cardio, Strength and Effort. A part shows only when its first load day is 28 or more days ago and its usual week is above 0. Cardio also needs heart rate on at least 3 in 4 of the workouts in the 35 days it looks at. Chips show only for two or more parts, in that order, and the card opens on the layout's `first` part, else Cardio, else the first part that shows. A card with no part, or a metric with no reading, is left out.
6. The hover shows the date, the day's value and the band's low and high, and opens on keyboard focus.
