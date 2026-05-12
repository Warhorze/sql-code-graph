WITH x AS (SELECT a FROM absent_one),
     y AS (SELECT b FROM absent_two)
SELECT x.a, y.b FROM x JOIN y ON x.a = y.b;
