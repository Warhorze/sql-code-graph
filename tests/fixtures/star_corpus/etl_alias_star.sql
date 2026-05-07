-- ETL using qualified alias star projection
CREATE TABLE BA.tgt_alias AS SELECT s.* FROM BA.src_table AS s;
