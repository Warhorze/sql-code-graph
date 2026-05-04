-- Mixed case identifiers (Snowflake case normalization)
SELECT
  Col1,
  COL2,
  col3,
  "QUOTED_COL4"
FROM
  MyTable
WHERE
  Col1 > 0
  AND COL2 IS NOT NULL
  AND col3 LIKE 'A%'
ORDER BY
  Col1 DESC,
  COL2 ASC;
