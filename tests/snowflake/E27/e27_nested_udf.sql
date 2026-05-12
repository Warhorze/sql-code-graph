-- outer_udf and inner_udf are intentionally undefined.
SELECT outer_udf(inner_udf(src_col)) AS dst_col FROM src;
