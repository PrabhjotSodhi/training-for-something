CREATE TABLE app_targets (
  connector       TEXT NOT NULL,
  remote_id       TEXT NOT NULL,
  title           TEXT NOT NULL,
  -- No declared type, so an app's numeric folder ID stays a number.
  folder,
  remote_version  TEXT NOT NULL,
  PRIMARY KEY (connector, remote_id)
);

ALTER TABLE plan_items ADD COLUMN send_into_json TEXT NOT NULL DEFAULT '{}'
  CHECK (json_valid(send_into_json));
