INSERT INTO wtfs_openstaande_orders (ma_aantal_op_order)
WITH bm_orders AS (
    SELECT SUM(ma_order_aantal / NULLIF(verhoudingsgetal, 0)) AS aantal_op_order
    FROM source_facts
    WHERE order_type = 'BM'
),
igdc_openstaand AS (
    SELECT SUM(ma_order_aantal / NULLIF(verhoudingsgetal, 0)) AS aantal_op_order
    FROM source_facts
    WHERE order_type = 'IGDC'
),
openstaand_combined AS (
    SELECT aantal_op_order FROM bm_orders
    UNION ALL
    SELECT aantal_op_order FROM igdc_openstaand
)
SELECT SUM(aantal_op_order) FROM openstaand_combined;
