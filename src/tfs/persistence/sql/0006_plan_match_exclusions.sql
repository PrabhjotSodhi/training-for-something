-- Source IDs deliberately have no foreign key: raw replay rebuilds imported rows.
CREATE TABLE plan_match_exclusions (
  item_id TEXT NOT NULL REFERENCES plan_items(id),
  record_id TEXT NOT NULL,
  PRIMARY KEY (item_id, record_id)
);
