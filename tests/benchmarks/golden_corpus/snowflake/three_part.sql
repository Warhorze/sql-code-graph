-- Three-part identifier: database.schema.table.column
SELECT
  analytics_db.raw_schema.events.event_id,
  analytics_db.raw_schema.events.user_id,
  analytics_db.processed_schema.users.email,
  analytics_db.processed_schema.users.created_at
FROM
  analytics_db.raw_schema.events
INNER JOIN
  analytics_db.processed_schema.users
ON
  analytics_db.raw_schema.events.user_id = analytics_db.processed_schema.users.id
WHERE
  analytics_db.raw_schema.events.event_date >= CURRENT_DATE() - 7;
