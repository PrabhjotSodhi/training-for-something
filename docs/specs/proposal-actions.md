# Proposal actions

The closed list of changes the AI may propose for v1.0. Save refuses anything not on it. Every Save, send and undo depends on this list.

## Shape of a proposal

```json
{
  "plan_version": 41,
  "why": "Your knee felt sore after Saturday. Moving the hard session gives it two more days.",
  "sources": [{"url": "https://...", "type": "meta-analysis"}],
  "actions": [
    {"action": "move_session", "item": "p-118", "to": "2026-10-31"},
    {"action": "change_session", "item": "p-120", "parts": [{"part_kind": "interval", "distance_m": 6000}]}
  ]
}
```

A proposal holds one or more actions. Save applies all of them in one transaction, or none.

## The actions

| Action | What it changes | Save checks | Sends to apps | Undo |
|---|---|---|---|---|
| `add_session` | A new plan item | Date not in the past, known family | Yes, apps whose `sends` list the family | Remove it |
| `move_session` | An item's date | Item exists and is not done | Yes, as an update | Move it back |
| `change_session` | An item's targets or label | Item exists and is not done | Yes, as an update | Restore the old version |
| `remove_session` | Marks an item removed | Item exists and is not done | Delete, or a `needs_you` to-do | Restore it |
| `set_goal` | Adds or edits a goal: type, date, target | Date in the future, known goal type | No | Restore the old goal |
| `set_limit` | A constraint, such as an injury, a day you cannot train, or a maximum session length | Known limit kind | No | Remove it |
| `log_meal` | A meal with foods, portions, how sure, and sources | Values in plausible ranges | No | Delete it |
| `accept_document_values` | Values read from a lab PDF or record, each with its page | Document exists, units known | No | Delete them |
| `correct_record` | A fix to a source record, such as its sport family | Record exists | No | Remove the correction |
| `link_sessions`, `unlink_sessions` | Two records counted as one workout, or split apart | Records exist | No | The opposite action |
| `set_source_order` | Your source list for one data type | Data type known | No | Restore the old order |
| `mark_done` | "I did this" for a plan item | Item exists | No | Clear it |
| `set_forecast` | A coach estimate for a goal with no code forecast: a range, the method, sources and date | Goal exists and has no code forecast | No | Restore the previous estimate |

Building a whole plan is a proposal made of many `add_session` actions and a `set_goal`.

## Rules

1. **Unknown action or field:** Save refuses the whole proposal.
2. **Stale proposal:** if `plan_version` is older than the current one, or a limit it depends on changed, Save refuses and asks the AI for a fresh one.
3. **Every Save writes one journal row** holding the proposal, who approved it (your chat's permission prompt or a TFS screen) and the time.
4. **Undo** is a new journal row with the opposite actions. If an app already received the change and cannot take it back, undo creates a `needs_you` to-do.
5. **The missed-session rule** may only propose `move_session`, `change_session`, `remove_session` and `add_session`, within the current week.

Later candidates, added only when a need appears: `log_note` (a free note on a day) and `set_preference` (such as "I prefer morning runs").

## Stored journal and versions

The journal's increasing integer `version` is the shared saved-state version.
Before the first save it is zero. A change to goals or limits advances it too,
so a proposal cannot use an outdated constraint. Each save records its proposal,
before-state, approval route (`chat` or `screen`), UTC time and format version.
Journal rows cannot be updated or deleted.

A save has a unique `request_id`. Retrying the same request and content returns
its original receipt; reusing the ID for different content is an error. The
application performs this comparison and validates the proposal again inside
the write transaction. The database enforces request uniqueness. These fields
record the client's approval route; they do not independently prove consent.

Goals store a type, target date and target JSON; limits store a kind and values
JSON. Their action handlers validate the allowed fields and units. Storage alone
does not make a Save tool available.

## Local session proposal API

The local application API currently supports `set_goal`, `set_limit`,
`add_session`, `move_session`, `change_session`, `remove_session` and `mark_done`. Other actions are refused
until their handlers arrive. This API does not expose an MCP Save tool yet.

Every proposal supplies `plan_version`, `why`, `sources` and a non-empty
`actions` list. Each source has `url` and `type`. Every action has `action`
and a stable `item` ID. Unknown fields are errors.

| Action | Additional fields |
|---|---|
| add_session | date, family, label, parts; optional goal_ids naming existing goals |
| move_session | to |
| change_session | label and/or parts |
| remove_session, mark_done | none |

Dates are local YYYY-MM-DD dates, today or later. The caller passes an aware
`now` in the person's local zone; journal time is stored in UTC. Parts are an
ordered list with `part_kind` and optional `label`, `duration_s`,
`distance_m`, `reps`, `weight_kg` and `avg_hr`. Numbers are finite and
non-negative; reps is a whole number. Array order becomes the stored sequence.

Preview calculates the batch in action order without writes, so adding then
changing an item in the same proposal works. Saving validates again inside the
write transaction and stores the final item state once per affected item,
together with its prior state in the journal. A stale proposal, invalid action
or database failure saves nothing. Repeated matching requests return the
original receipt. Edits of done, removed or replaced items are refused.


## Local undo

`undo(db_path, target_version, request_id=..., approved_by=..., now=...)`
reverses the latest local plan, goal or limit save. `target_version` must still be the
current journal version. An intervening save makes it stale; selective undo of
older history is not supported yet.

Undo restores the recorded previous item and parts, or marks an added item
removed. It increments each item's version and appends a journal entry with
`{"undo_version": target_version}` as its operation and the state before undo
as its before-state. Earlier journal rows remain unchanged. The same request
ID and target returns the original receipt; a different target fails. The
latest undo can itself be undone.

This local API refuses sent or unknown items and unsupported journal operation
formats. External delivery reversal and client permission prompts are separate
work. No MCP undo tool is exposed yet.


## Goals, limits and combined history

`set_goal` requires `action`, `item`, `type`, `date` and `target`. The date is
strictly future. Types are `marathon`, `ironman`, `hyrox`, `powerlifting`,
`lean_physique`, `longevity` and `custom`. Use custom for another goal; the
engine does not need sport-specific code. A target has a non-empty
`description` and optionally a finite, non-negative `value` together with a
non-empty `unit`. Units are preserved as supplied, not converted or used for
forecasts here. These fields do not authorize diagnosis or clinician-target changes.

`set_limit` requires `action`, `item`, `kind` and `values`. Supported kinds:

| Kind | Exact values fields |
|---|---|
| injury | description: non-empty user-recorded text |
| unavailable_day | date: YYYY-MM-DD, today or later |
| max_session_length | minutes: finite, positive number |

Goal, limit and session IDs have separate namespaces. Reusing a goal/limit ID
edits it. A session can reference a goal established earlier in the same
proposal. Preview returns `items`, `goals` and `limits` without writes. Saving
any of them advances the global version, so a changed limit invalidates an old
proposal even when no session changed.

New journal rows use format 2: before-state has separate `items`, `goals` and
`limits` maps, each keyed by ID. An absent prior row is null. Undo reads old
format-1 session rows without changing them. It restores edited goals/limits,
deletes newly created goals/limits, and marks newly created sessions removed.
All states and the new journal receipt commit or roll back together. The latest
undo can be undone, including restoring a goal or limit removed by undo.
