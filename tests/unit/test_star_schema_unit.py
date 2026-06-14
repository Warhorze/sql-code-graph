"""Unit tests for STAR_SOURCE graph schema additions.

These tests verify schema.py enum values, schema.cypher DDL, and db info output.
"""

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend

# ---------------------------------------------------------------------------
# T-02 — SCHEMA_VERSION bump and RelType.STAR_SOURCE enum
# ---------------------------------------------------------------------------


def test_rel_type_star_source_enum_value():
    """RelType must expose STAR_SOURCE = 'STAR_SOURCE' after T-02."""
    from sqlcg.core.schema import RelType

    assert hasattr(RelType, "STAR_SOURCE")
    assert RelType.STAR_SOURCE == "STAR_SOURCE"


def test_schema_version_matches_current_migration():
    """SCHEMA_VERSION must be '11' after the JOIN_COL_RESOLVE edge-table addition.

    Each schema-shape change forces a re-index (no backward compatibility — CLAUDE.md);
    bumping this constant is the gate. Bumped 10 -> 11 for the ``JOIN_COL_RESOLVE``
    edge table added by plan/sprints/bugfix_lineage_correctness_validation.md §PR-5
    (post-index join-column resolution).
    """
    from sqlcg.core.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == "11", (
        f"Expected SCHEMA_VERSION='11', got {SCHEMA_VERSION!r}. "
        "Bump SCHEMA_VERSION in src/sqlcg/core/schema.py when the schema shape changes."
    )


def test_star_source_rel_table_in_schema_cypher():
    """schema.cypher must contain the STAR_SOURCE REL TABLE definition."""
    from sqlcg.core.schema import SCHEMA_DDL

    assert "STAR_SOURCE" in SCHEMA_DDL, (
        "STAR_SOURCE REL TABLE not found in schema.cypher. "
        "Add it after the COLUMN_LINEAGE block in schema.cypher."
    )
    # Verify it has the required columns
    assert "qualifier" in SCHEMA_DDL
    assert "target_table" in SCHEMA_DDL
    assert "confidence" in SCHEMA_DDL


def test_star_source_table_created_in_fresh_db():
    """init_schema() on a fresh in-memory DB must create the STAR_SOURCE REL TABLE."""
    db = DuckDBBackend(":memory:")
    try:
        db.init_schema()
        rows = db.run_read(
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = 'main'",
            {},
        )
        table_names = [r["name"] for r in rows]
        assert "STAR_SOURCE" in table_names, (
            f"STAR_SOURCE not in schema tables. Found: {table_names}"
        )
    finally:
        db.close()


def test_star_source_rel_has_correct_properties():
    """The STAR_SOURCE REL TABLE must accept qualifier, target_table, confidence writes."""
    db = DuckDBBackend(":memory:")
    try:
        db.init_schema()
        # Create source nodes for the edge
        db.upsert_node(
            "SqlQuery",
            "q:0",
            {
                "id": "q:0",
                "file_path": "f.sql",
                "statement_index": 0,
                "sql": "INSERT INTO t SELECT * FROM s",
                "kind": "INSERT",
                "target_table": "BA.tgt",
                "parse_failed": False,
                "confidence": 1.0,
                "parsing_mode": "sqlglot",
            },
        )
        db.upsert_node(
            "SqlTable",
            "BA.src",
            {
                "qualified": "BA.src",
                "name": "src",
                "catalog": "",
                "db": "BA",
                "kind": "TABLE",
                "defined_in_file": "",
            },
        )
        # Upsert the STAR_SOURCE edge — must not raise
        db.upsert_edge(
            "SqlQuery",
            "q:0",
            "SqlTable",
            "BA.src",
            "STAR_SOURCE",
            {"qualifier": "<unqualified>", "target_table": "BA.tgt", "confidence": 0.8},
        )
        rows = db.run_read(
            'SELECT qualifier AS q, target_table AS tgt, confidence AS c FROM "STAR_SOURCE"',
            {},
        )
        assert len(rows) == 1
        assert rows[0]["q"] == "<unqualified>"
        assert rows[0]["tgt"] == "BA.tgt"
        assert abs(rows[0]["c"] - 0.8) < 1e-6
    finally:
        db.close()


# ---------------------------------------------------------------------------
# db info surfaces star metrics
# ---------------------------------------------------------------------------


def test_star_metrics_in_info_output():
    """db info must print STAR_SOURCE edges and STAR_EXPANSION lineage edge counts."""
    from unittest.mock import patch

    from typer.testing import CliRunner

    from sqlcg.cli.commands.db import app

    runner = CliRunner()

    # db info routes every SQL read through run_read_routed (read_client), so
    # the mock must target that function rather than a backend object.
    def run_read_side_effect(query, _params):
        if "SchemaVersion" in query and "version" in query and "count" not in query.lower():
            return [{"version": "6"}]
        if "indexed_sha" in query:
            return [{"sha": None}]
        if '"Repo"' in query and "path" in query and "count" not in query.lower():
            # No repo path -> freshness block is skipped.
            return []
        if "STAR_SOURCE" in query and "COLUMN_LINEAGE" not in query:
            return [{"n": 3}]
        if "STAR_EXPANSION" in query:
            return [{"n": 6}]
        if "Repo" in query and "count" in query.lower():
            return [{"count": 1}]
        if "SqlQuery" in query and "count" in query.lower():
            return [{"count": 10}]
        if "SqlColumn" in query and "count" in query.lower():
            return [{"count": 50}]
        if "COLUMN_LINEAGE" in query and "count" in query.lower():
            return [{"count": 25}]
        return [{"count": 0}]

    with patch("sqlcg.cli.commands.db.run_read_routed", side_effect=run_read_side_effect):
        result = runner.invoke(app, ["info"])

    assert result.exit_code == 0
    assert "STAR_SOURCE edges" in result.output, (
        "db info must print 'STAR_SOURCE edges' count. "
        "Add the COUNT_STAR_SOURCES_QUERY call in the db info command."
    )
    assert "STAR_EXPANSION lineage edges" in result.output, (
        "db info must print 'STAR_EXPANSION lineage edges' count. "
        "Add the COUNT_STAR_EXPANSIONS_QUERY call in the db info command."
    )
    # Values must appear — exact substring match not just "no exception"
    assert "3" in result.output
    assert "6" in result.output


@pytest.mark.xfail(reason="db info schema version warning not yet implemented", strict=True)
def test_db_info_warns_on_outdated_schema_version():
    """db info must warn when the stored schema version is older than SCHEMA_VERSION."""
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from sqlcg.cli.commands.db import app

    runner = CliRunner()

    with patch("sqlcg.cli.commands.db.get_backend") as mock_get_backend:
        mock_backend = MagicMock()
        mock_backend.__enter__.return_value = mock_backend
        mock_backend.__exit__.return_value = None
        # Simulate a v1 database against a v2 binary
        mock_backend.get_schema_version.return_value = "1"
        mock_backend.run_read.return_value = [{"count": 1}]
        mock_get_backend.return_value = mock_backend

        result = runner.invoke(app, ["info"])

    assert result.exit_code == 0
    assert "outdated" in result.output.lower() or "re-index" in result.output.lower(), (
        "db info must warn that the schema is outdated when stored version != SCHEMA_VERSION"
    )
