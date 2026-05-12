SELECT DECODE(status, 'A', active_col, 'B', backup_col, default_col) AS result
FROM src;
