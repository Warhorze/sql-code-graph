-- Customers aggregation table (references raw_orders)
CREATE TABLE customers AS
SELECT
  customer_id,
  COUNT(*) AS order_count,
  SUM(amount) AS lifetime_value,
  MAX(created_at) AS last_order_date
FROM
  raw_orders
WHERE
  status = 'completed'
GROUP BY
  customer_id;
