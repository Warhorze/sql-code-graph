SELECT
    current_timestamp() AS ts_col,
    NEXTVAL('my_seq') AS seq_col,
    'static value' AS lit_col
FROM dual;
