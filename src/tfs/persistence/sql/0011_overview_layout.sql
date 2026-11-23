CREATE TABLE overview_layout (
  id          INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
  layout_json TEXT NOT NULL CHECK (json_valid(layout_json))
);
