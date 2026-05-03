-- Raw orders data source (minimal jaffle_shop DDL)
CREATE TABLE raw_orders (
  id INT NOT NULL PRIMARY KEY,
  customer_id INT NOT NULL,
  amount DECIMAL(10, 2),
  status VARCHAR(50),
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);
