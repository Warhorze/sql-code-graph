CREATE OR REPLACE VIEW mart_fullmoon_reviews AS
SELECT
    listing_id,
    date,
    reviewer_name,
    comments,
    sentiment,
    dim_listing_id,
    host_id,
    host_name
FROM fct_reviews
WHERE EXTRACT(DAY FROM date) >= 13 AND EXTRACT(DAY FROM date) <= 14;
