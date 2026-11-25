CREATE TEMP VIEW coach_v1_day_facts AS
SELECT local_date, metric, value, unit, source,
  CASE WHEN local_date IN (SELECT local_date FROM main.rebuild_dates)
    THEN 'importing' ELSE coverage END AS coverage
FROM main.day_facts;

CREATE TEMP VIEW coach_v1_workouts AS
SELECT g.id AS workout_id, g.local_date, g.start_utc,
  (SELECT r.utc_offset_min FROM main.session_group_members m
    JOIN main.source_records r ON r.id = m.record_id
    WHERE m.group_id = g.id AND r.deleted_at IS NULL AND r.start_utc = g.start_utc
    ORDER BY r.id LIMIT 1) AS utc_offset_min,
  json_extract(g.chosen_json, '$.family.value') AS family,
  json_extract(g.chosen_json, '$.label.value') AS label,
  round(json_extract(g.chosen_json, '$.duration_s.value') / 60.0, 1) AS duration_min,
  round(json_extract(g.chosen_json, '$.distance_m.value') / 1000.0, 2) AS distance_km,
  json_extract(g.chosen_json, '$.active_energy_kcal.value') AS active_energy_kcal,
  json_extract(g.chosen_json, '$.avg_hr.value') AS avg_hr,
  json_extract(g.chosen_json, '$.max_hr.value') AS max_hr,
  coalesce(
    (SELECT connector FROM main.source_records
      WHERE id = json_extract(g.chosen_json, '$.duration_s.record_id')),
    (SELECT connector FROM main.source_records
      WHERE id = json_extract(g.chosen_json, '$.distance_m.record_id'))
  ) AS source,
  (SELECT connector FROM main.source_records
    WHERE id = json_extract(g.chosen_json, '$.active_energy_kcal.record_id'))
    AS active_energy_source,
  (SELECT connector FROM main.source_records
    WHERE id = json_extract(g.chosen_json, '$.avg_hr.record_id')) AS avg_hr_source,
  (SELECT connector FROM main.source_records
    WHERE id = json_extract(g.chosen_json, '$.max_hr.record_id')) AS max_hr_source,
  (SELECT group_concat(connector, ', ') FROM (
    SELECT DISTINCT r.connector FROM main.session_group_members m
    JOIN main.source_records r ON r.id = m.record_id
    WHERE m.group_id = g.id AND r.deleted_at IS NULL
    ORDER BY r.connector)) AS sources,
  coalesce(
    (SELECT max(json_extract(r.values_json, '$.duration_s'))
        - min(json_extract(r.values_json, '$.duration_s'))
        > 0.1 * json_extract(g.chosen_json, '$.duration_s.value')
      FROM main.session_group_members m JOIN main.source_records r ON r.id = m.record_id
      WHERE m.group_id = g.id AND r.deleted_at IS NULL)
    OR (SELECT max(json_extract(r.values_json, '$.distance_m'))
        - min(json_extract(r.values_json, '$.distance_m'))
        > 0.1 * json_extract(g.chosen_json, '$.distance_m.value')
      FROM main.session_group_members m JOIN main.source_records r ON r.id = m.record_id
      WHERE m.group_id = g.id AND r.deleted_at IS NULL),
    0
  ) AS conflict
FROM (
  SELECT original.id, original.local_date, original.start_utc,
    (SELECT json_group_object(field.key, json(field.value))
      FROM json_each(original.chosen_json) field
      JOIN main.source_records r ON r.id = json_extract(field.value, '$.record_id')
      WHERE r.deleted_at IS NULL) AS chosen_json
  FROM main.session_groups original
) g
WHERE EXISTS (
  SELECT 1 FROM main.session_group_members m JOIN main.source_records r ON r.id = m.record_id
  WHERE m.group_id = g.id AND r.deleted_at IS NULL
);

CREATE TEMP VIEW coach_v1_workout_parts AS
SELECT g.id AS workout_id, p.seq, p.part_kind, p.label, p.reps, p.weight_kg,
  round(p.duration_s / 60.0, 1) AS duration_min,
  round(p.distance_m / 1000.0, 2) AS distance_km,
  p.avg_hr, r.connector AS source
FROM main.session_groups g
JOIN main.session_parts p ON p.record_id = (
  SELECT m.record_id FROM main.session_group_members m
  JOIN main.source_records live ON live.id = m.record_id AND live.deleted_at IS NULL
  WHERE m.group_id = g.id
  ORDER BY (SELECT count(*) FROM main.session_parts WHERE record_id = m.record_id) DESC,
    m.record_id
  LIMIT 1)
JOIN main.source_records r ON r.id = p.record_id;

CREATE TEMP VIEW coach_v1_sets_by_muscle AS
SELECT date(g.local_date, '-' || ((strftime('%w', g.local_date) + 6) % 7) || ' days') AS week_start,
  coalesce(m.muscle, 'other') AS muscle, count(*) AS sets
FROM main.session_groups g
JOIN main.session_parts p ON p.record_id = (
  SELECT x.record_id FROM main.session_group_members x
  JOIN main.source_records live ON live.id = x.record_id AND live.deleted_at IS NULL
  WHERE x.group_id = g.id
  ORDER BY (SELECT count(*) FROM main.session_parts WHERE record_id = x.record_id) DESC,
    x.record_id
  LIMIT 1)
JOIN main.source_records r ON r.id = p.record_id
LEFT JOIN main.exercise_muscles m ON m.connector = r.connector
  AND m.template_id = json_extract(p.extras_json, '$.exercise_template_id')
WHERE p.part_kind = 'set' AND coalesce(json_extract(p.extras_json, '$.set_type'), '') != 'warmup'
GROUP BY 1, 2;

CREATE TEMP VIEW coach_v1_sources AS
SELECT connector, enabled, last_received_at, last_error,
  (SELECT MAX(COALESCE(end_utc, start_utc)) FROM main.source_records
    WHERE connector = sources.connector AND deleted_at IS NULL) AS covered_until,
  last_contact_at
FROM main.sources;


CREATE TEMP VIEW coach_v1_plan_items AS
SELECT p.id AS plan_item_id, p.version, p.plan_version, p.local_date, p.start_hint,
  p.family, p.label, p.goal_ids_json, p.why, p.sources_json, p.send_into_json,
  CASE WHEN m.item_id IS NOT NULL THEN 'done' ELSE p.status END AS status,
  m.group_id AS matched_workout_id, m.reason AS match_reason,
  (SELECT json_group_array(id) FROM (
    SELECT r.id FROM main.session_group_members gm JOIN main.source_records r ON r.id=gm.record_id
    WHERE gm.group_id=m.group_id AND r.deleted_at IS NULL ORDER BY r.id
  )) AS matched_record_ids_json
FROM main.plan_items p LEFT JOIN main.plan_matches m ON m.item_id=p.id
AND EXISTS (SELECT 1 FROM main.session_group_members gm JOIN main.source_records r ON r.id=gm.record_id
  WHERE gm.group_id=m.group_id AND r.deleted_at IS NULL)
AND NOT EXISTS (SELECT 1 FROM main.session_group_members gm JOIN main.source_records r ON r.id=gm.record_id
  JOIN main.rebuild_dates d ON d.local_date=r.local_date WHERE gm.group_id=m.group_id);

CREATE TEMP VIEW coach_v1_plan_parts AS
SELECT plan_item_id, seq, part_kind, label, reps, weight_kg,
  duration_s / 60.0 AS duration_min, distance_m / 1000.0 AS distance_km,
  avg_hr, extras_json
FROM main.session_parts
WHERE plan_item_id IS NOT NULL;

CREATE TEMP VIEW coach_v1_plan_state AS
SELECT coalesce(max(version), 0) AS plan_version FROM main.journal;


CREATE TEMP VIEW coach_v1_goals AS
SELECT id AS goal_id, type, local_date, target_json FROM main.goals;

CREATE TEMP VIEW coach_v1_forecasts AS
SELECT goal_id, low, high, unit, method, sources_json, local_date,
  'Coach estimate' AS label FROM main.coach_forecasts;

CREATE TEMP VIEW coach_v1_overview AS
SELECT layout_json FROM main.overview_layout;

CREATE TEMP VIEW coach_v1_limits AS
SELECT id AS limit_id, kind, values_json FROM main.limits;

CREATE TEMP VIEW coach_v1_app_targets AS
SELECT connector, remote_id, title, folder, remote_version FROM main.app_targets;


CREATE TEMP VIEW coach_v1_deliveries AS
SELECT j.id AS op_id, j.item_id AS plan_item_id, j.plan_version, j.connector,
  j.status, j.attempts, j.next_attempt_at, j.created_at,
  json_extract(j.result_json, '$.message') AS message,
  json_extract(j.result_json, '$.auth_failed') AS auth_failed,
  coalesce(json_extract(j.result_json, '$.remote_id'), r.remote_id) AS remote_id,
  CASE WHEN json_extract(j.result_json, '$.remote_id') IS NOT NULL
    THEN json_extract(j.result_json, '$.remote_version') ELSE r.remote_version END AS remote_version,
  json_extract(j.result_json, '$.manual_action') AS manual_action,
  json_extract(j.result_json, '$.safe_to_replace') AS safe_to_replace
FROM main.jobs j LEFT JOIN main.remote_links r
  ON r.item_id = j.item_id AND r.connector = j.connector;

CREATE TEMP VIEW coach_v1_scores AS
SELECT local_date, score, value, low, high, inputs_json, formula_version FROM main.scores;
