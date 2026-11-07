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

CREATE TEMP VIEW coach_v1_sources AS
SELECT connector, enabled, last_received_at, last_error,
  (SELECT MAX(COALESCE(end_utc, start_utc)) FROM main.source_records
    WHERE connector = sources.connector AND deleted_at IS NULL) AS covered_until
FROM main.sources;


CREATE TEMP VIEW coach_v1_plan_items AS
SELECT id AS plan_item_id, version, plan_version, local_date, start_hint,
  family, label, goal_ids_json, why, sources_json, status
FROM main.plan_items;

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

CREATE TEMP VIEW coach_v1_limits AS
SELECT id AS limit_id, kind, values_json FROM main.limits;


CREATE TEMP VIEW coach_v1_deliveries AS
SELECT j.id AS op_id, j.item_id AS plan_item_id, j.plan_version, j.connector,
  j.status, j.attempts, j.next_attempt_at, j.created_at,
  json_extract(j.result_json, '$.message') AS message,
  json_extract(j.result_json, '$.auth_failed') AS auth_failed,
  r.remote_id, r.remote_version
FROM main.jobs j LEFT JOIN main.remote_links r
  ON r.item_id = j.item_id AND r.connector = j.connector;
