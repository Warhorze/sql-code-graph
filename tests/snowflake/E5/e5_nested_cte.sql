WITH inner_cte AS (SELECT a FROM absent_root),
     outer_cte AS (SELECT a FROM inner_cte)
SELECT a FROM outer_cte;
