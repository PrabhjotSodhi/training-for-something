CREATE TABLE plan_matches (
  item_id     TEXT NOT NULL PRIMARY KEY REFERENCES plan_items(id) ON DELETE CASCADE,
  group_id    TEXT NOT NULL UNIQUE REFERENCES session_groups(id) ON DELETE CASCADE,
  reason      TEXT NOT NULL CHECK (reason IN ('reference', 'date_family'))
);

INSERT OR IGNORE INTO rebuild_dates
SELECT local_date FROM session_groups;
