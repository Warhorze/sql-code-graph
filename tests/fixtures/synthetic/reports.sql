-- Report queries using views
SELECT
    customer_id,
    customer_name,
    COUNT(*) as order_count,
    SUM(amount) as total_amount
FROM customer_orders
WHERE order_date >= '2023-01-01'
GROUP BY customer_id, customer_name
ORDER BY total_amount DESC;

SELECT
    order_id,
    product_name,
    quantity,
    price,
    line_total
FROM order_details
WHERE quantity > 0
ORDER BY order_id, product_id;
