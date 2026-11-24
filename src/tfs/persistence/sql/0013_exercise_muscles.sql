CREATE TABLE exercise_muscles (
  connector    TEXT NOT NULL,
  template_id  TEXT NOT NULL,
  muscle       TEXT NOT NULL,
  PRIMARY KEY (connector, template_id)
);
