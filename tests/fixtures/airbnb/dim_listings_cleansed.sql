CREATE OR REPLACE VIEW dim_listings_cleansed AS
SELECT
    l.id,
    l.name,
    l.room_type,
    l.minimum_nights,
    l.price,
    h.id AS host_id,
    h.name AS host_name,
    h.is_superhost
FROM src_listings l
JOIN src_hosts h ON l.host_id = h.id;
