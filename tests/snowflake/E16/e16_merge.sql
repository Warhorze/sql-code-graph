MERGE INTO dst USING src ON dst.id = src.id
WHEN MATCHED THEN UPDATE SET col = src.col_a
WHEN NOT MATCHED THEN INSERT (col) VALUES (src.col_b);
