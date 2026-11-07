CREATE TABLE journal (
  version         INTEGER PRIMARY KEY AUTOINCREMENT CHECK (version > 0),
  request_id      TEXT NOT NULL UNIQUE,
  proposal_json   TEXT NOT NULL CHECK (json_valid(proposal_json)),
  before_json     TEXT NOT NULL CHECK (json_valid(before_json)),
  approved_by     TEXT NOT NULL CHECK (approved_by IN ('chat', 'screen')),
  saved_at        TEXT NOT NULL,
  format_version  INTEGER NOT NULL DEFAULT 1
);
CREATE TRIGGER journal_no_replace BEFORE INSERT ON journal
WHEN EXISTS (SELECT 1 FROM journal
             WHERE request_id = NEW.request_id OR version = NEW.version)
BEGIN
  SELECT RAISE(ABORT, 'journal is append-only');
END;
CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal
BEGIN
  SELECT RAISE(ABORT, 'journal is append-only');
END;
CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal
BEGIN
  SELECT RAISE(ABORT, 'journal is append-only');
END;

CREATE TABLE goals (
  id           TEXT NOT NULL PRIMARY KEY,
  type         TEXT NOT NULL,
  local_date   TEXT NOT NULL,
  target_json  TEXT NOT NULL CHECK (json_valid(target_json))
);

CREATE TABLE limits (
  id           TEXT NOT NULL PRIMARY KEY,
  kind         TEXT NOT NULL,
  values_json  TEXT NOT NULL CHECK (json_valid(values_json))
);

CREATE TABLE plan_items (
  id             TEXT NOT NULL PRIMARY KEY,
  version        INTEGER NOT NULL CHECK (version > 0),
  plan_version   INTEGER NOT NULL REFERENCES journal (version),
  local_date     TEXT NOT NULL,
  start_hint     TEXT,
  family         TEXT NOT NULL,
  label          TEXT NOT NULL,
  goal_ids_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(goal_ids_json)),
  why            TEXT NOT NULL,
  sources_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(sources_json)),
  status         TEXT NOT NULL DEFAULT 'saved'
                 CHECK (status IN ('saved', 'sent', 'done', 'skipped', 'replaced',
                                   'missed', 'unknown', 'removed'))
);

CREATE TABLE session_parts_new (
  record_id     TEXT REFERENCES source_records (id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  part_kind     TEXT NOT NULL,
  label         TEXT,
  duration_s    REAL,
  distance_m    REAL,
  reps          INTEGER,
  weight_kg     REAL,
  avg_hr        REAL,
  extras_json   TEXT,
  plan_item_id  TEXT REFERENCES plan_items (id) ON DELETE CASCADE,
  CHECK ((record_id IS NOT NULL) != (plan_item_id IS NOT NULL)),
  UNIQUE (record_id, seq),
  UNIQUE (plan_item_id, seq)
);
INSERT INTO session_parts_new
  (record_id, seq, part_kind, label, duration_s, distance_m, reps, weight_kg, avg_hr, extras_json)
SELECT record_id, seq, part_kind, label, duration_s, distance_m, reps, weight_kg, avg_hr, extras_json
FROM session_parts;
DROP TABLE session_parts;
ALTER TABLE session_parts_new RENAME TO session_parts;
