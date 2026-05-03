-- COPY INTO @stage with SELECT (STAGE node, LOADS_FROM_STAGE rel)
COPY INTO @my_stage/data FROM (
  SELECT
    id,
    name,
    email,
    created_at
  FROM
    customer_source
  WHERE
    created_at >= CURRENT_DATE() - INTERVAL '30 days'
)
FILE_FORMAT = (TYPE = 'PARQUET')
OVERWRITE = TRUE;
