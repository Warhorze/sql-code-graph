# Airbnb dbt Fixture Parse Quality Report
## Per-Layer Breakdown
### Raw
- Files: 3
- Parsed: 3
- Errored: 0
- Success Rate: 100.0%

### Staging
- Files: 3
- Parsed: 3
- Errored: 0
- Success Rate: 100.0%

### Dim Fact
- Files: 3
- Parsed: 3
- Errored: 0
- Success Rate: 100.0%

### Mart
- Files: 1
- Parsed: 1
- Errored: 0
- Success Rate: 100.0%

## Summary
- Total Files: 10
- Total Parsed: 10
- Total Errored: 0
- Overall Success Rate: 100.0%

## Files with Scope-Building Failures
(Note: These are syntactically valid SQL files where sqlglot's scope builder failed. Typically CREATE TABLE DDL without SELECT clauses.)
- tests/fixtures/airbnb/raw_reviews.sql
- tests/fixtures/airbnb/raw_listings.sql
- tests/fixtures/airbnb/raw_hosts.sql

## Table and Lineage Counts
- Tables Found: 10
- Lineage Edges Created: 12

## Parsing Mode Distribution
- sqlglot: 10
