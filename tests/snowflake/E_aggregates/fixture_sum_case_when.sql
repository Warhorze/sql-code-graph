INSERT INTO db.s.t (s) SELECT SUM(CASE WHEN flag = 1 THEN amount END) AS s FROM db.s.src;
