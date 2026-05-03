-- Enriched orders with customer metrics (references raw_orders and customers)
CREATE TABLE orders AS
SELECT
  o.id,
  o.customer_id,
  o.amount,
  o.status,
  o.created_at,
  c.order_count,
  c.lifetime_value,
  c.last_order_date
FROM
  raw_orders o
INNER JOIN
  customers c
ON
  o.customer_id = c.customer_id
WHERE
  o.status = 'completed';
