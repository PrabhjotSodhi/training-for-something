CREATE TABLE rules (
  id             TEXT NOT NULL PRIMARY KEY,
  revision       INTEGER NOT NULL CHECK (revision >= 1),
  period_minutes INTEGER NOT NULL CHECK (period_minutes >= 1),
  next_due       TEXT NOT NULL,
  hourly_cap     INTEGER NOT NULL CHECK (hourly_cap >= 1),
  cap_hour       TEXT,
  cap_runs       INTEGER NOT NULL DEFAULT 0 CHECK (cap_runs >= 0)
);
CREATE INDEX rules_due ON rules(next_due);
CREATE TABLE rule_runs (
  id         TEXT NOT NULL PRIMARY KEY,
  rule_id    TEXT NOT NULL REFERENCES rules(id),
  created_at TEXT NOT NULL
);
