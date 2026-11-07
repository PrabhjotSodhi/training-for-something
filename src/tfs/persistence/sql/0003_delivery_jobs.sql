CREATE TABLE jobs (
  id               TEXT NOT NULL PRIMARY KEY,
  item_id          TEXT NOT NULL REFERENCES plan_items(id),
  plan_version     INTEGER NOT NULL REFERENCES journal(version),
  connector        TEXT NOT NULL,
  payload_json     TEXT NOT NULL CHECK (json_valid(payload_json)),
  status           TEXT NOT NULL CHECK (status IN
                   ('pending', 'sending', 'in_doubt', 'sent', 'not_sent',
                    'unknown', 'conflict', 'needs_you', 'cancelled')),
  attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at  TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  result_json      TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  UNIQUE (item_id, connector, plan_version)
);
CREATE INDEX jobs_due ON jobs(status, next_attempt_at);
CREATE TRIGGER jobs_immutable_payload
BEFORE UPDATE OF id, item_id, plan_version, connector, payload_json, created_at ON jobs
BEGIN
  SELECT RAISE(ABORT, 'approved job payload is immutable');
END;
