-- CREATE PROCEDURE with embedded DML (confidence=0.3 for body)
CREATE OR REPLACE PROCEDURE process_daily_batch(input_date DATE)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
  rows_processed INT := 0;
BEGIN
  INSERT INTO processed_events
  SELECT
    event_id,
    user_id,
    event_type,
    CURRENT_TIMESTAMP() AS processed_at
  FROM
    raw_events
  WHERE
    DATE(event_time) = input_date;

  SET rows_processed := @@ROWCOUNT;

  RETURN 'Processed ' || rows_processed || ' rows';
END;
$$;
