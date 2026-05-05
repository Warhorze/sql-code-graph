-- Base tables for synthetic test fixtures
CREATE TABLE customers (
    id INT PRIMARY KEY,
    name VARCHAR,
    email VARCHAR
);

CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT REFERENCES customers(id),
    order_date DATE,
    amount DECIMAL(10, 2)
);

CREATE TABLE products (
    id INT PRIMARY KEY,
    name VARCHAR,
    price DECIMAL(10, 2)
);

CREATE TABLE order_items (
    id INT PRIMARY KEY,
    order_id INT REFERENCES orders(id),
    product_id INT REFERENCES products(id),
    quantity INT
);
