-- QUALIFY clause for window function filtering
SELECT
  customer_id,
  order_date,
  amount,
  ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) AS recency_rank
FROM
  orders
QUALIFY
  ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) = 1
ORDER BY
  customer_id;
