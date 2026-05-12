SELECT f.value::STRING AS col
FROM tbl, LATERAL FLATTEN(input => arr) f;
