CREATE OR REPLACE VIEW fct_reviews AS
SELECT
    r.listing_id,
    r.date,
    r.reviewer_name,
    r.comments,
    r.sentiment,
    d.id AS dim_listing_id,
    d.host_id,
    d.host_name
FROM src_reviews r
JOIN dim_listings_cleansed d ON r.listing_id = d.id;
