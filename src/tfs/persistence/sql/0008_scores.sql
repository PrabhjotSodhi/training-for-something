CREATE TABLE scores (
  local_date       TEXT NOT NULL,
  score            TEXT NOT NULL,
  value            INTEGER,
  low              INTEGER,
  high             INTEGER,
  inputs_json      TEXT NOT NULL,
  formula_version  TEXT NOT NULL,
  PRIMARY KEY (local_date, score)
);
