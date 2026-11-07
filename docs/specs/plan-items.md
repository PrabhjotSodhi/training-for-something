# Plan items

One planned session, its fields, its states and how a workout marks it done. Save, sending, done matching, the missed-session rule and undo all depend on it.

## What a plan item is

One planned session on one date. It reuses the session shape from HLD section 5.2, so any sport works with no new code: a family, the app's label, and planned parts in the same `session_parts` columns (duration, distance, intensity, sets, reps, weight).

```text
Tue 28 Oct   run       "8 km easy"        part: 8 km, zone 2
Thu 30 Oct   strength  "Push A"           parts: bench 4x6 @ 80 kg, press 3x8 @ 40 kg
Sat  1 Nov   run       "Half marathon"    part: 21.1 km, goal pace 4:58 /km
```

There is **one training calendar**, shared by every goal. A session can serve several goals (a long run counts for a marathon and for longevity) and still counts once in total load.

## Fields

| Field | Meaning |
|---|---|
| `id` | Stable id, kept across versions of the same item |
| `version` | Goes up each time the item changes |
| `plan_version` | The whole plan's version when this item was saved. Every saved change raises it. Proposals record it, and Save refuses a proposal built on an older one. |
| `local_date`, `start_hint` | The day, and an optional time of day |
| `family`, `label` | Sport family and the plan's own name for the session |
| parts | Planned targets, in `session_parts` |
| `goal_ids` | Goals this session serves |
| `why`, `sources` | The coach's reason and its labelled sources |
| `status` | See below |
| remote links | One per app it was sent to: connector, remote id, remote version. Kept on the item, so an update finds the right object. |

## States

```text
 proposed ──Save──> saved ──send──> sent ──workout arrives──> done
    │                 │               │
    │ refused         │ no matching workout, 12 h after the day
    v                 v               v
 discarded          missed  <─── or unknown, if the source is stale

 Any saved or sent item can become:
   skipped   (you said so)
   replaced  (a newer version saved)
   done      (you tapped "I did this")
```

- **unknown** is not missed. It holds while the source that would show the workout is stale, or a workout with no sport family arrived that day.
- **replaced:** saving a new version cancels the old version's unsent jobs. For an app that already has the old version, the new version goes out as an update, or a `needs_you` to-do when the app cannot update or delete.

## Done matching

From HLD section 11, in this order:

1. A workout whose plan reference (for example Hevy's `routine_id`) matches the item's remote id.
2. Otherwise, same local date and same family. When two items of one family share a day, the nearest start time wins.
3. You can always mark done by hand, or unlink a wrong match.

Sending a plan never marks it done.

## Stored plan state

`plan_items` holds the current version of each planned session. Its `plan_version`
references the journal entry that last changed it. Removed items use the status
`removed`; undo history lives in the journal rather than duplicate current rows.

`session_parts` has exactly one owner: `record_id` for an imported workout or
`plan_item_id` for a planned session. Sequence numbers are unique within each
owner. Both owners use the same part fields and stored units. Deleting an imported
record cannot delete planned parts.
