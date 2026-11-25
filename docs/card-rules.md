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

1. The page has no sidebar. Its content is centred, at most 1440 px wide, with 24 px on the left and right.
2. A slim bar above the plate shows the name "Training For Something" in text, left aligned with the content. The right end stays empty.
3. The page follows the system setting for light or dark.
4. Desktop width comes first.

## Phone

Below 700 px, cards fill one column with 16 px page margins. The goal name and date sit above the road, followed by the forecast and Today rows.

Keep all seven days on one row. Show up to two small session tiles per day with their sport and state styles. Show any extra sessions as +N. Tap the day to read all its sessions below the row. Put weekly totals underneath. Hover details must also open on tap. Give each touch target at least 44 px in both directions.

## Colour

| Part | Light | Dark |
|---|---|---|
| Page | `#f5f5f7` | `#0a0a0b` |
| Text | `#1c1c1e` | `#f5f5f7` |
| Muted text | `#6b6b70` | `#98989f` |
| Lines | `#e5e5ea` | `#2c2c2e` |

Every card takes one goal colour from the layout's plate, orange with no plate: orange `#FF9500` light and `#FF9F0A` dark, blue `#007AFF` and `#0A84FF`, green `#34C759` and `#30D158`, purple `#AF52DE` and `#BF5AF2`. Deeper, lighter and tint shades are worked out from it, never picked by hand. Text on a goal fill is dark for orange and green and white for blue and purple.

## Surfaces

1. Two surfaces only: the page and the card.
2. No shadows on any surface.
3. A card has the card fill (`#ffffff` in light, `#1c1c1e` in dark), a 1 px line edge, a 20 px radius and 24 px padding.

## Grid

1. Cards sit in two columns with a 20 px gap, and 20 px under the plate. The week card spans both columns.
2. Cards in one row share one height.
3. While the page loads, empty cards keep their size so nothing jumps.
4. A failed load shows one muted line that says the page cannot reach TFS and why.
5. A card type the page does not know is skipped and logged in the console.

## Goal plate

1. The plate sits above the cards, full width, at least 42 percent of the screen height, with a 20 px radius and no shadow.
2. The background is an animated mesh gradient from `@paper-design/shaders-react`, pinned to one exact version. With reduced motion on, the gradient stands still.
3. Each goal colour is a fixed set of colours: orange `#5A1E00 #FF8A00 #C2453A`, blue `#071F4A #0A7BFF #1E8FA0`, green `#0B3324 #2FBF5A #2A8A80`, purple `#24093E #B04FE0 #5257D6`. A shade layer of the darkest colour sits over the gradient so pale text passes 4.5:1.
4. Three groups sit on the gradient with no cards and no takeaway sentence. The forecast with its range and the goal mark is at the top left. The Today rows, in the order the layout lists them, are at the top right. The timeline runs along the bottom: goal name and date together at the right above the line's end, "Week N of M" and "N days to go" below it at each end.
5. The forecast number is 96 px, 700 weight. All other text uses the type scale.
6. No forecast shows "No forecast yet". A row with no value today shows "No data". No plan items hides "Week N of M".
7. Week 1 is the week the plan starts. M is the weeks from the plan start to the goal date, rounded up.

## First-run panel

1. While no goal is saved, the panel sits where the plate goes. Once any goal exists the panel is gone.
2. It uses the plate itself in the default colour, orange: the same gradient, shade, size and white text. Its three groups fill the plate's three places.
3. Top left, where the forecast sits: "Your latest workout", the duration as the 96 px number with "min", and under it one line with the type, date and source app, such as "Run · Wed 25 Nov · Hevy". With no workouts the number is replaced by "No workouts yet" in the number style. It never shows a zero.
4. Top right, where Today sits: "Next step", one row "Set your first training goal in your AI tool", then a solid white "Copy first message" button with dark text, the one solid shape on the plate. The button copies the same first message as setup and reads "Copied. Paste it into your AI tool." for 2 seconds. If the copy fails, the message shows as text under the button.
5. Along the bottom, where the timeline sits: one caption line per turned on app with no records, "Waiting for the first data from {app}." A phone app adds "Keep the TFS window open." Nothing shows there when no app is waiting.

## This week card

1. The card spans both columns. It shows Monday to Sunday of the current week, one column per day, today's column marked.
2. Each session is one tile. Its height follows its time. The tile shows the session name and its amount with the sport's unit: km for run and ride, m for swim, sets for strength. Time shows only in the hover, which also opens on keyboard focus.
3. Tiles tell the states apart by look. A done session fills with the goal colour: strength is hatched, ride is the deep shade, swim the light shade. A planned session is grey. A planned session today is outlined in the goal colour. A missed session has a dashed outline and reads "Missed".
4. A sport with no unit, or a session with no distance or sets, shows minutes.
5. A done session counts its own distance, or every set it holds. A workout matched to a plan item shows once, on that item's tile. A workout with no plan item is its own done tile.
6. A planned session with no matched workout is missed 12 hours after its day ends. Until then, and while any enabled source is stale or a workout with no sport family arrived that day, it stays planned. A missed session still counts as planned.
7. A day with no session says "Rest".
8. A column on the right shows sessions done this week, like "3 of 8 sessions". Under it each sport shows done of planned with a bar, like "22 of 78 km". With no plan item that week the count is only the sessions done, and a sport with nothing planned shows only what is done, like "7.2 km", with no bar.

## Sleep card

1. The headline is the average hours asleep over the last 7 nights, like "7 h 35 min a night". With no night in the 7 it says "No data".
2. One row per night, oldest first, ending today: weekday, a bar in the goal colour and the time. Today's weekday is in the goal colour. A night belongs to the day the person wakes up.
3. A night with no data says "No data", has no bar and stays out of the average.
4. A dashed line marks the saved sleep goal, labelled like "8 h goal". With no saved goal there is no line.
5. A night with sleep stages splits its bar into deep, core, REM and awake, left to right, in shades of the goal colour with awake in grey. The bar is as long as all its stages together. A stage the source left out has no part. A night without stages keeps the plain bar.
6. Hovering or focusing a night with stages shows its date, time asleep and its deep, REM and core time.

## Trend card

1. One component draws resting heart rate, HRV, weight, VO2 max and training load: the value, a line over the shaded normal band, a dot on the latest reading, and the band edges as the only y labels. The stretch of line outside the band uses the goal colour from the layout's plate, orange with no plate.
2. The chart shows the 30 days ending today. A day with no reading is empty. Readings are joined by a line when they are 1 day apart for resting heart rate and HRV, and up to 14 days apart for weight and VO2 max. Weight and VO2 max also show a dot on each reading.
3. The band is the readiness rule: the median of the previous 42 days from the same source, plus and minus the spread, with at least 21 of those days. A day with no band draws no band and the line still shows.
4. Training load never shows load points. The line is the last 7 days over the usual week, with the band at 0.88 to 1.1. The value reads like "+16% last 7 days vs your usual". State words are Below normal, Normal and Above normal.
5. Training load has one part each for Cardio, Strength and Effort. A part shows only when its first load day is 28 or more days ago and its usual week is above 0. Cardio also needs heart rate on at least 3 in 4 of the workouts in the 35 days it looks at. Chips show only for two or more parts, in that order, and the card opens on the layout's `first` part, else Cardio, else the first part that shows. A card with no part, or a metric with no reading, is left out.
6. The hover shows the date, the day's value and the band's low and high, and opens on keyboard focus.

## Sets by muscle card

1. The headline is the number of working sets this week, like "21 sets". With none it says "No sets this week" and shows no rows.
2. One row per muscle, most sets first: name, a bar in the goal colour scaled to the top muscle and the count. A muscle with no sets has no row.
3. Only the primary muscle of each exercise counts. Warmup sets do not count. An exercise with no muscle tag counts under "other".

## Lift progress card

1. The picker lists exercises with weight in at least one working set in the last 90 days. It ranks them by working set count and uses each exercise's template id. It opens on the saved exercise if available, then falls back to the most logged. With no eligible exercise, the card stays hidden.
2. Heaviest shows one point per workout in the last 12 weeks. The latest point shows its date and weight in kg. Two workouts on the same day keep separate points.
3. Weekly volume shows the current week and the previous 11 weeks, starting on Monday. Each bar shows kg lifted, the sum of working-set weight times reps. A week without sessions stays empty.
4. Both controls change only the current view. A person can save an exercise through an approved `set_overview` proposal.
