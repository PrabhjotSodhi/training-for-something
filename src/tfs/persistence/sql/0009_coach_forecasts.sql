CREATE TABLE coach_forecasts (
  goal_id       TEXT NOT NULL PRIMARY KEY,
  low           REAL NOT NULL,
  high          REAL NOT NULL,
  unit          TEXT NOT NULL,
  method        TEXT NOT NULL,
  sources_json  TEXT NOT NULL CHECK (json_valid(sources_json)),
  local_date    TEXT NOT NULL
);
