WITH x AS (SELECT a FROM table_not_in_schema)
SELECT a FROM x;
