ALTER TABLE jobs ADD COLUMN resolved_by_version INTEGER REFERENCES journal(version);
CREATE TRIGGER jobs_resolution_immutable BEFORE UPDATE OF resolved_by_version ON jobs
WHEN OLD.resolved_by_version IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'a reported external deletion is immutable');
END;
