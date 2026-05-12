CREATE TEMP TABLE tmp_a AS
SELECT
    afzet,
    gemiddelde_vrd,
    afzet / NULLIF(gemiddelde_vrd, 0) AS omloopsnelheid
FROM stg_a;

CREATE TEMP TABLE tmp_b AS
SELECT
    afzet,
    gemiddelde_vrd,
    afzet / NULLIF(gemiddelde_vrd, 0) AS omloopsnelheid
FROM stg_b;

INSERT INTO persistent_target (afzet, gemiddelde_vrd)
SELECT afzet, gemiddelde_vrd FROM tmp_a
UNION ALL
SELECT afzet, gemiddelde_vrd FROM tmp_b;
