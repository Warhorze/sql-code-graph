INSERT INTO db.s.t (s) SELECT SUM(amount) AS s FROM db.s.src;
