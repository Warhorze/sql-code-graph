-- Column reference with reserved word after colon (Snowflake edge case)
SELECT
  t:type,
  t:value,
  t:timestamp,
  t:id
FROM
  event_data t
WHERE
  t:type IN ('click', 'purchase')
  AND t:timestamp >= CURRENT_TIMESTAMP() - INTERVAL '1 day'
ORDER BY
  t:timestamp DESC;
