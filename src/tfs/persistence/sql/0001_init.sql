CREATE TABLE files (
  sha256          TEXT NOT NULL PRIMARY KEY,
  arrival_seq     INTEGER NOT NULL UNIQUE,
  connector       TEXT NOT NULL,
  format          TEXT NOT NULL,
  ext             TEXT NOT NULL,
  size_bytes      INTEGER NOT NULL,
  received_at     TEXT NOT NULL,
  reader_version  TEXT,
  status          TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'reading', 'ok', 'partial', 'unreadable', 'needs_ai', 'failed', 'missing')),
  message         TEXT
);

CREATE TABLE sources (
  connector         TEXT NOT NULL PRIMARY KEY,
  enabled           INTEGER NOT NULL DEFAULT 1,
  cursor_json       TEXT,
  last_attempt_at   TEXT,
  last_contact_at   TEXT,
  last_received_at  TEXT,
  last_error        TEXT
);

CREATE TABLE source_records (
  id              TEXT NOT NULL PRIMARY KEY,
  connector       TEXT NOT NULL,
  stream          TEXT NOT NULL,
  key             TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('session', 'measurement', 'event', 'document')),
  revision_at     TEXT NOT NULL,
  arrival_seq     INTEGER NOT NULL,
  file_sha256     TEXT NOT NULL REFERENCES files (sha256),
  reader_version  TEXT NOT NULL,
  start_utc       TEXT NOT NULL,
  end_utc         TEXT,
  utc_offset_min  INTEGER,
  local_date      TEXT NOT NULL,
  label           TEXT,
  family          TEXT,
  metric          TEXT,
  values_json     TEXT NOT NULL,
  origin_app      TEXT,
  origin_device   TEXT,
  plan_ref        TEXT,
  deleted_at      TEXT,
  UNIQUE (connector, stream, key)
);
CREATE INDEX source_records_day ON source_records (local_date, kind);
CREATE INDEX source_records_metric_day ON source_records (metric, local_date);
CREATE INDEX source_records_file ON source_records (file_sha256);

CREATE TABLE source_deletes (
  connector    TEXT NOT NULL,
  stream       TEXT NOT NULL,
  key          TEXT NOT NULL,
  revision_at  TEXT NOT NULL,
  arrival_seq  INTEGER NOT NULL,
  PRIMARY KEY (connector, stream, key)
);

CREATE TABLE session_parts (
  record_id    TEXT NOT NULL REFERENCES source_records (id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,
  part_kind    TEXT NOT NULL,
  label        TEXT,
  duration_s   REAL,
  distance_m   REAL,
  reps         INTEGER,
  weight_kg    REAL,
  avg_hr       REAL,
  extras_json  TEXT,
  PRIMARY KEY (record_id, seq)
);

CREATE TABLE session_groups (
  id           TEXT NOT NULL PRIMARY KEY,
  local_date   TEXT NOT NULL,
  family       TEXT,
  start_utc    TEXT NOT NULL,
  chosen_json  TEXT NOT NULL
);

CREATE TABLE session_group_members (
  group_id   TEXT NOT NULL REFERENCES session_groups (id) ON DELETE CASCADE,
  record_id  TEXT NOT NULL UNIQUE REFERENCES source_records (id) ON DELETE CASCADE,
  PRIMARY KEY (group_id, record_id)
);

CREATE TABLE day_facts (
  local_date       TEXT NOT NULL,
  metric           TEXT NOT NULL,
  value            REAL NOT NULL,
  unit             TEXT NOT NULL,
  source           TEXT NOT NULL,
  coverage         TEXT NOT NULL CHECK (coverage IN ('full', 'partial')),
  formula_version  TEXT NOT NULL,
  PRIMARY KEY (local_date, metric)
);

CREATE TABLE rebuild_dates (
  local_date  TEXT NOT NULL PRIMARY KEY
);
