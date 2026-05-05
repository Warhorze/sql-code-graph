-- Views based on base tables
CREATE VIEW customer_orders AS
SELECT
    c.id as customer_id,
    c.name as customer_name,
    o.id as order_id,
    o.order_date,
    o.amount
FROM customers c
LEFT JOIN orders o ON c.id = o.customer_id;

CREATE VIEW order_details AS
SELECT
    o.id as order_id,
    o.customer_id,
    oi.product_id,
    p.name as product_name,
    oi.quantity,
    p.price,
    (oi.quantity * p.price) as line_total
FROM orders o
JOIN order_items oi ON o.id = oi.order_id
JOIN products p ON oi.product_id = p.id;
