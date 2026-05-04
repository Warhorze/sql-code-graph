-- LATERAL FLATTEN for nested array traversal (transform=FLATTEN)
SELECT
  t.id,
  t.name,
  f.value AS flattened_item,
  f.index
FROM
  table_with_arrays t,
  LATERAL FLATTEN(INPUT => t.nested_array) f
WHERE
  f.value IS NOT NULL
ORDER BY
  t.id,
  f.index;
