CREATE OR REPLACE VIEW dim_hosts_cleansed AS
SELECT
    id,
    name,
    is_superhost,
    created_at,
    updated_at
FROM src_hosts;
