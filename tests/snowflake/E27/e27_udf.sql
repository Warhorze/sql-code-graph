-- my_udf is intentionally undefined: no CREATE FUNCTION statement exists
-- in this fixture or in any loaded schema. The missing UDF definition is
-- deliberate — the test pins parser behaviour when a UDF reference cannot
-- be resolved.
SELECT my_udf(src_col) AS dst_col FROM src;
