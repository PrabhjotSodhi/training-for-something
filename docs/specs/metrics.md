# Metrics

Daily facts, scores and forecasts. Every formula carries a version. Coefficients live in configuration, never in code.

## Rules for every number

1. **Missing is unknown, never zero.** A score needs its minimum inputs, or it shows "not enough data" and names what is missing.
2. **Show the working.** Each score shows its inputs, source device, baseline days and a likely range.
3. **Never make finer data from summaries.** A daily HRV value cannot become beat-to-beat data. An average heart rate cannot become time in zones.
4. **Keep definitions apart.** HRV keeps its statistic type (RMSSD or SDNN) and window. Active energy and total energy stay separate.
5. **A new device starts a new baseline.** Values from two devices are never spliced into one baseline.

## Daily facts

| Fact | Meaning | Missing rule |
|---|---|---|
| Sleep | Hours asleep, on the day you wake up | 0 hours is missing |
| Resting heart rate | The device's daily value | 0 is missing |
| HRV | The device's nightly value, with its type | 0 is missing |
| Steps, active energy, total energy | One source per day (HLD section 7.3) | A partial day says partial |
| Weight, body fat | Latest reading that day | None that day is missing |
| VO2 max | Latest device estimate, with its source | Older than 30 days shows its age |

## Scores

| Score | Method | Needs | Shows |
|---|---|---|---|
| **Readiness** | Your HRV, resting heart rate, sleep and recent load against your own normal. Robust z-scores over a 42 day baseline, weighted 0.35, 0.25, 0.25 and 0.15, mapped so 50 means your normal. | 21 valid days, and at least two of HRV, resting heart rate and sleep | "68, likely 61 to 74", inputs, baseline days |
| **Training load** | Three channels, never added together. Cardio: heart rate TRIMP. Strength: working sets x reps x weight, with RIR or RPE when logged. Perceived: RPE x minutes. | A workout with duration. Heart rate for cardio load. | Each channel against your own recent normal |
| **Recovery time** | Each channel's load decays over time. Cardio and strength are kept apart. | Six weeks of history for a personal decay rate | A range, such as "18 to 30 h". Strength may say "unknown". |
| **Workout heart rate** | Zones from heart rate reserve. Max heart rate measured, or estimated as 211 minus 0.64 x age and marked estimated. Heart rate recovery at 60 and 120 seconds. | A heart rate stream | Time in each zone, recovery |
| **VO2 max** | Your device's latest VO2 max, placed on the HUNT study's age and sex reference curve. Never shown below or above the reference range. | Age, reference sex, VO2 max | "38, high for your age", percentile, source, confidence. No decimals. |

## Forecasts

The website promises a forecast range per goal. Two kinds exist.

**1. Code forecasts**, for goal types with a known formula. Exact and repeatable.

| Goal type | Method | Shows |
|---|---|---|
| Endurance race (marathon, half, Ironman legs) | Riegel's formula from recent best efforts at shorter distances, widened by how old and how far those efforts are | A finish time range |
| Powerlifting | Estimated one-rep max (Epley) from recent top sets, per lift, summed | A total range |
| Lean physique | Body weight trend over the last 4 weeks, extended to the goal date | A weight range on the date |
| Longevity | VO2 max now and its trend | VO2 max and percentile |

Ironman adds a fixed transition estimate to the three leg forecasts.

**2. Coach estimates**, for any other goal, such as Hyrox. The coach researches a method with the research rules (HLD section 9.4), applies it to the user's data through the ask TFS tool, and proposes a range with the `set_forecast` action. The user saves it like any change. It shows as "Coach estimate" with its method, sources and date, and the coach refreshes it at each weekly review.
