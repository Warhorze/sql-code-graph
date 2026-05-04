-- Snowflake colon :: cast operator
SELECT
  id::VARCHAR AS id_str,
  amount::DECIMAL(10,2) AS amount_decimal,
  created_at::DATE AS created_date,
  raw_json::OBJECT AS parsed_object
FROM
  raw_events
WHERE
  event_type::VARCHAR LIKE 'purchase_%'
ORDER BY
  created_at::TIMESTAMP DESC;
