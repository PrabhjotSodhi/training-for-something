CREATE TEMP VIEW coach_v1_day_facts AS
SELECT local_date, metric, value, unit, source,
  CASE WHEN local_date IN (SELECT local_date FROM main.rebuild_dates)
    THEN 'importing' ELSE coverage END AS coverage
FROM main.day_facts;
