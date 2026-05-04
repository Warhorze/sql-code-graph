CREATE OR REPLACE VIEW src_reviews AS
SELECT
    listing_id,
    date,
    reviewer_name,
    comments,
    sentiment
FROM raw_reviews
WHERE sentiment != 'NOT VERIFIED';
