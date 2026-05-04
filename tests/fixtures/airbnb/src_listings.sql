CREATE OR REPLACE VIEW src_listings AS
SELECT
    id,
    listing_url,
    name,
    room_type,
    minimum_nights,
    host_id,
    price,
    created_at,
    updated_at
FROM raw_listings
WHERE minimum_nights > 0;
