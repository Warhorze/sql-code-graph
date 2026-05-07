-- ETL using SELECT * (star projection)
INSERT INTO BA.tgt_table SELECT * FROM BA.src_table;
