CREATE TABLE remote_links (
  item_id          TEXT NOT NULL REFERENCES plan_items(id),
  connector        TEXT NOT NULL,
  remote_id        TEXT NOT NULL,
  remote_version   TEXT NOT NULL,
  last_job_id      TEXT NOT NULL REFERENCES jobs(id),
  PRIMARY KEY (item_id, connector)
);
