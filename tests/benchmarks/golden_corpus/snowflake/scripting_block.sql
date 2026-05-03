-- Snowflake scripting block with embedded DML (confidence=0.3)
BEGIN
  LET insert_count INT := 0;
  FOR record IN (SELECT id, name FROM source_table) DO
    INSERT INTO target_table (id, name) VALUES (record.id, record.name);
    SET insert_count := insert_count + 1;
  END FOR;
  RETURN insert_count;
END;
