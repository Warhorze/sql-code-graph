CREATE OR REPLACE VIEW src_hosts AS
SELECT
    id,
    name,
    is_superhost,
    created_at,
    updated_at
FROM raw_hosts;
