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
| `remove_session` | Marks an item removed | Item exists and is not done | Delete, or a `needs_you` to-do | Undo pending removal; explicitly restore after a deletion report |
| `restore_session` | Restores a removed plan on an explicit date | No linked copy or unresolved deletion task; date not past | Create a new copy in approved apps | Remove it |
| `report_delivery_deleted` | Records the person's report and retires the exact link | Exact outstanding deletion task; single-action proposal | No | Report is permanent; restore the plan explicitly |
| `set_goal` | Adds or edits a goal: type, date, target | Date in the future, known goal type | No | Restore the old goal |
| `set_limit` | A constraint, such as an injury, a day you cannot train, or a maximum session length | Known limit kind | No | Remove it |
| `log_meal` | A meal with foods, portions, how sure, and sources | Values in plausible ranges | No | Delete it |
| `accept_document_values` | Values read from a lab PDF or record, each with its page | Document exists, units known | No | Delete them |
| `correct_record` | A fix to a source record, such as its sport family | Record exists | No | Remove the correction |
| `link_sessions`, `unlink_sessions` | Two records counted as one workout, or split apart | Records exist | No | The opposite action |
| `set_source_order` | Your source list for one data type | Data type known | No | Restore the old order |
| `unlink_plan_match` | Rejects one workout match for a plan item | Exact current source-record IDs | No | Restore the match exclusions |
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
`add_session`, `move_session`, `change_session`, `remove_session`, `restore_session`, `mark_done`, `unlink_plan_match` and
`report_delivery_deleted`. Other actions are refused
until their handlers arrive. The MCP tools below expose this same API.

Every proposal supplies `plan_version`, `why`, `sources` and a non-empty
`actions` list. Each source has `url` and `type`. Every action has `action`
and a stable `item` ID. Unknown fields are errors.

| Action | Additional fields |
|---|---|
| add_session | date, family, label, parts; optional goal_ids naming existing goals, start_hint |
| move_session, restore_session | to; optional start_hint |
| change_session | at least one of label, parts, start_hint |
| remove_session, mark_done | none |
| report_delivery_deleted | connector, op_id, remote_id; single-action proposal |
| unlink_plan_match | record_ids: complete sorted unique non-empty list from matched_record_ids_json |

`start_hint` is a local 24-hour HH:MM time, or null for no preferred time.
Omission on add means null; omission on move/change keeps the previous hint.
Explicit null clears it. Seconds and timezone offsets are not accepted.

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
original receipt. Ordinary edits of done, removed or replaced items are refused;
restore_session is the explicit route for a removed item.


## Undo and its preview

`undo(db_path, target_version, request_id=..., approved_by=..., now=..., delivery_targets=...)`
reverses the latest supported plan, goal or limit save. `target_version` must still be the
current journal version. An intervening save makes it stale; selective undo of
older history is not supported yet.

Undo restores the recorded previous item and parts, or marks an added item
removed. It increments each item's version and appends a journal entry with
`{"undo_version": target_version}` as its operation and the state before undo
as its before-state. Earlier journal rows remain unchanged. The same request
ID and target returns the original receipt; a different target fails. The
latest undo can itself be undone.

`preview_undo(db_path, target_version, now=...)` returns the exact restored
content and delivery targets without writes. Undo uses the same latest-only
rule and refuses unsupported history or unresolved delivery. Existing delivered
edit support is defined below; it is not a general remote deletion feature.


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


## Chat MCP tools

- `propose(proposal)` validates and returns the predicted items, goals and
  limits without writing.
- `save(proposal, request_id)` submits the full reviewed proposal through the
  client permission prompt. The server records the route as chat and uses the
  computer's aware local time; there is no AI-supplied approval boolean.
- `preview_undo(target_version)` shows the restored state and affected apps
  without writing.
- `undo(target_version, request_id, delivery_targets)` reverses the latest
  supported change through the same client approval boundary.

The tools advertise explicit input schemas. Unknown arguments and invalid
proposals fail without a write. Reuse a request ID only for an identical retry.
A JSON-RPC notification has no reply ID and is ignored before tool dispatch,
so it cannot save or undo. Tool annotations describe behavior to the client;
they do not enforce approval. Always-allow client settings can skip prompts.
These tools perform no provider delivery yet. Real installed-client approval
and refusal remain a separate proof from synthetic protocol tests.


## Queued delivery approval

Preview returns `delivery_targets`, a map from item ID to sorted, unique connector
IDs, and `delivery_apps`, their display labels. Targets come from enabled sources
whose manifest accepts the final item's family. Copy that exact target map into
the proposal shown for Save. Omission means no destinations; Save refuses if the
current targets differ. A newly connected app cannot silently receive a plan.

Save cancels superseded pending/not-sent jobs and queues the exact final item in
the same transaction as its plan and journal. Removed items target only existing
linked copies; unlinked removals and done items enqueue nothing. Jobs keep a stable operation ID and immutable approved payload; secrets
never enter them. Provider sends are not enabled yet.

Journal format 3 adds optional `jobs` before-state: job IDs mapped to their prior
status and next-attempt time, or null for a newly created job. Entries without
job changes still use format 2. Existing format 1/2 history stays readable and is
never rewritten. Undo cancels the new jobs and restores previous pending jobs
with their original IDs and payloads. Undo itself records the inverse, so redo
works without inventing a second delivery.

A move or change after confirmed delivery retains the acknowledged job and
remote link, and creates a new approved job with a new operation ID. The item
returns to saved while the update waits. Preview and Save require previously
linked apps to be enabled and capable of receiving the plan; otherwise reconnect
first. Approval covers the desired item and destination apps. A delivery confirmed
between preview and Save can turn the next dispatch into an update of the known
remote object without changing that approved content or those destinations.
Manual mark_done retains confirmed history and cancels pending replacements.

Sending, in-doubt, unknown, conflict and needs-you jobs still block overlapping
changes. Resolved deletion instructions and their retired acknowledged copies
remain history and do not block later work. Delivered-edit undo uses approved
updates; removal follows the manual task flow below. Invented delivery tests do
not satisfy real-provider proof #29.


## Unlink a wrong plan match

`unlink_plan_match` is separate from `unlink_sessions`, which separates duplicate
imported workouts. It excludes the supplied source records from matching this
plan. Preview and Save require the complete active evidence currently shown by
`matched_record_ids_json`; changed or pending evidence requires a fresh read and
preview. The choice uses stable source IDs, never a rebuilt workout group ID.

Preview returns `match_exclusions` as item/record_ids entries. Unlink alone changes
no planned targets, delivery jobs or remote links. It preserves manual done.
Unlink followed by a plan edit in the same proposal follows action order; the
edit keeps the ordinary delivery rules. Another eligible workout may still
complete the plan.

Journal format 4 adds an optional `exclusions` map from item ID to its prior
source-ID list. Other changes retain formats 2/3, and older history stays readable.
Latest-only undo/redo restores exclusions in the same transaction and rebuilds
matches. The global version advances; unlink alone does not change the item's
payload version. Request-ID retries return the original receipt.


## Undoing an existing delivered edit

Before saving a change to a linked item, journal format 5 records an optional
`delivery_links` before-state map: item ID to connector IDs and their remote IDs.
This proves which copies existed when the change was saved. A lower job version
alone is not that proof. Formats 1–4 remain readable and are never rewritten;
older rows lacking this evidence cannot undo a delivered edit.

For an update to a linked item, recorded prior remote copies must exactly match
the current linked copies. Missing evidence, a changed remote identity, disabled
app or unresolved job requires reconciliation. A newly connected unlinked app
is not an undo destination. If undo removes a delivered addition or restoration,
preview names its current linked copies and the approved reversal creates
removal tasks. Unresolved tasks block undo until the person reports deletion;
then use explicit restore_session. Manual completion reversal is described below.

Preview returns restored items/goals/limits, optional match exclusions,
`delivery_targets` and `delivery_apps`, with no writes. Show that content and
copy the exact destination map into the approved undo request. Omitting targets
means no external destinations. The transaction rechecks global version,
recorded copies, current capabilities and targets. An invalid undo changes
nothing, and an identical request retry returns its receipt.

For remote items, cancel only pending/not-sent replacements and queue a fresh
operation with the restored content. Preserve acknowledged jobs and current
remote links/versions. Never restore historical sent statuses or reuse an old
remote operation ID. Local-only items keep original-ID unsent undo. Undo records
its own before-state, so undo-of-undo creates a fresh compensation when needed.
The existing connector update checks the current provider version before writing.
No provider delete, new table or automatic conflict resolution is introduced.


## Manual deletion and explicit restoration

Removal keeps sent receipts and links and queues an immutable removed payload
for each linked destination. Hevy returns needs_you, manual_action=delete and
the remote ID, without any HTTP request. The existing credential and capability
gates still apply before dispatch; a disconnected app may need reconnecting
before TFS can produce this instruction. TFS never claims it deleted the routine.

report_delivery_deleted requires item, connector, removal op_id and remote_id.
It must be the only action. Preview and Save validate the exact unresolved
structured removal result and current link; authentication problems, unknown
outcomes, conflicts and generic needs_you messages are not eligible. Reporting
an existing task works after disconnecting the app. Approval records the person's
statement, not a provider check. Preview labels verification as user_reported.

In one transaction, append the report to the journal, set the job's nullable
resolved_by_version to that entry and retire only the identified link. The
original needs_you outcome, result, payload and earlier sent receipts remain.
The resolution cannot be reset. A matching request retry returns its receipt.
Undo of this external report is refused rather than erasing the fact or reviving
the link. Other journal rows and their versions are unchanged.

restore_session requires a removed item, no remaining linked copy and no
unresolved delivery. The explicit to date must be today or later; existing
targets are retained and an optional start_hint follows move_session rules.
Preview's creates_new_copies names restored items with outgoing destinations.
Save queues new operation IDs; subsequent dispatch has no retired remote ID.
Undo before delivery cancels that work. Undo after delivery uses another removal
task. This keeps reversible desired plan content separate from reported external
actions, without skipping journal rows or silently undoing an older removal.

coach_v1_deliveries exposes manual_action, resolved_by_version and resolution
(user_reported_deleted). Its remote fields prefer the job's recorded result,
so an old task remains identifiable after link retirement or later restoration.
Filter resolved_by_version IS NULL when looking for outstanding manual tasks.


## Undoing manual completion

Supported mark_done history and its undo identify a completion reversal. Clearing
manual completion preserves the prior saved/sent status and checks prior link
identity. It restores only the original cancelled pending/not-sent jobs, with
unchanged IDs and payloads; an acknowledged or uncertain job cannot be rewritten
from an old job-state map. With no previously cancelled work, it queues nothing.
Disconnected restored work remains paused until reconnection.

Undo preview includes restored_deliveries with each original operation ID,
connector/app label, exact item payload, status and next-attempt time. Show these
alongside restored plan content and any fresh delivery_targets before approval.
The immutable journal binds these original job IDs; undo validates their current
states again within the transaction. Existing request IDs and stale-version
checks still apply. This preview also identifies resumed jobs in local-only undo.

Redo to done behaves like forward mark_done: preserve current links and every
acknowledged receipt, cancel only pending/not-sent work and queue nothing.
Sending or unresolved work still blocks it. No historical-link comparison is
needed for this local completion change, including when a first delivery finished
between undo and redo. It never replays a cancelled status onto a newly sent job.


## Correcting a proven unsent mapping failure

A connector may return needs_you with safe_to_replace=true only when this fresh
operation failed its own mapping/validation before any write. Hevy uses this for
unknown or ambiguous exercise names, unsupported targets and missing sets.
Reconciliation never carries this flag; neither do unknown, conflict,
authentication, manual-deletion or unproven provider failures.

The coach delivery view exposes this evidence as safe_to_replace=1. It permits
an approved correction, not an automatic retry. Preview and Save may supersede
the flagged operation, retain its immutable payload/result, mark it cancelled
and queue a fresh approved operation. Acknowledged jobs and links stay intact.
Local unsent undo can restore the original flagged task and redo can restore the
original queued correction, without inventing another send. Other unresolved
outcomes still require reconciliation; no user-supplied flag bypasses this rule.
