-- IDENTIFIER() for dynamic column reference
SELECT
  table_name,
  IDENTIFIER(column_name) AS dynamic_col,
  COUNT(*) AS cnt
FROM
  dynamic_tables
GROUP BY
  table_name,
  column_name
HAVING
  COUNT(*) > 10
ORDER BY
  table_name;
