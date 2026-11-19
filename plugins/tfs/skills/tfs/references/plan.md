# Plan changes

Use `query` to read the current `coach_v1_plan_state` and relevant plan, goal and limit views. Build a proposal with that exact global version, a reason, labelled source URLs and supported actions. The tool schemas describe the fields. Planned parts use seconds, metres, kg and beats/minute; never confuse these with the read views' minutes and km.

Call `propose` to validate and preview. It saves nothing. Show the exact changes, their reason, sources and delivery apps to the person. Copy the returned `delivery_targets` map into the proposal so Save includes only those destinations. If destinations change, preview and request approval again. Submit the same full proposal to `save` only through the AI client's user permission prompt. Use a unique request ID for a new save, and reuse that ID with identical content if retrying an uncertain response. If the user refuses, do not call Save or use another route to write. A stale proposal needs a fresh read and preview, not a changed version pasted onto an old proposal.

To take back a saved change, propose the opposite change, such as `remove_session` for an added session or `move_session` back to the old date, and request Save approval like any other change. Removing a delivered session creates a manual deletion task. Never describe a removal as deleting a routine from an app. Provider writes remain disabled pending verification.

The client permission prompt is the consent boundary. An AI-generated approval flag is not consent, and always-allow client settings can skip the prompt. The installed AI CLI is trusted software with the person's operating-system access; TFS tools are not an OS sandbox. Keep save configured to require approval. Do not edit files or use another route to change the person's data.

Supported local changes are set_goal, set_limit, add_session, move_session, change_session, remove_session, restore_session, mark_done and unlink_plan_match. Sending plans to apps, corrections and other action types are not available through these tools yet.

For a wrong automatic match, read `matched_record_ids_json` from the plan view. Propose `unlink_plan_match` with the item and that complete sorted list as `record_ids`. Explain that this prevents those workouts from completing this plan, survives replay, and leaves manual completion intact. If the match changes before Save, read and preview again.

For a manual deletion task, show the exact app and remote ID from `coach_v1_deliveries`. TFS has not deleted it and no longer tracks it. The person deletes it in the app by hand, and TFS does not check.

To bring a removed plan back, propose `restore_session` with an explicit `to` date and optional start_hint. Show that this creates a new copy in the previewed apps. Tell the person to delete the old copy in the app first, or the app holds two. Provider writes remain disabled until their separate verification.
