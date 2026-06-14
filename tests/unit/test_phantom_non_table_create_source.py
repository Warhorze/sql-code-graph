"""Non-table CREATE DDL must not emit its own object name as a source node.

Guards the phantom-source-node bugfix (#161)
([plan doc](plan/sprints/sprint_snowflake_lineage_patterns.md), PR-B).

#159 fixed the *kind label* (non-table CREATE -> OTHER). The phantom SOURCE node
persisted: for a `CREATE SEQUENCE ba.s` / `CREATE STAGE` / `CREATE FILE FORMAT`,
`build_scope` returns None so source extraction fell to `_fallback_table_scan`,
which scooped the object's OWN name (`ba.s`) into `QueryNode.sources` -> it then
leaked into `ParsedFile.referenced_tables` (the unconditional
`referenced_tables.extend(query_node.sources)` at ansi_parser.py:246 /
snowflake_parser.py:784 / :944), creating a bogus SqlTable node + SELECTS_FROM
edge in the graph.

The fix is a single source-gate inside `AnsiParser._parse_statement`: when the
statement is an `exp.Create` whose `kind` is not TABLE/VIEW, `sources = []`. The
gate is keyed off the sqlglot AST (`stmt.kind`), so it is independent of the
`_classify` path. CREATE TABLE/VIEW ... AS SELECT keep their kind and therefore
keep their real sources (control tests below).

Each assertion checks observable output (the `sources` list / `referenced_tables`
list contents), not "no exception raised".
"""

from pathlib import Path

import sqlglot

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser


def _parse(sql: str) -> "tuple":
    """Parse ``sql`` (snowflake dialect) and return (ParsedFile, first QueryNode)."""
    parser = AnsiParser(SchemaResolver(dialect="snowflake"))
    parsed = parser.parse_file(Path("t.sql"), sql)
    first = parsed.statements[0] if parsed.statements else None
    return parsed, first


def _source_ids(node) -> list[str]:
    return [s.full_id for s in node.sources]


def _ref_ids(parsed) -> list[str]:
    return [r.full_id for r in parsed.referenced_tables]


class TestNonTableCreateEmitsNoPhantomSource:
    """SEQUENCE / STAGE / FILE FORMAT names never appear as sources."""

    def test_create_sequence_has_no_source(self):
        """CREATE SEQUENCE: object name not scooped into sources (AC-B1)."""
        parsed, node = _parse("CREATE SEQUENCE IF NOT EXISTS ba.wsdh_s1")
        assert node is not None
        assert node.sources == []
        assert "ba.wsdh_s1" not in _ref_ids(parsed)
        assert parsed.referenced_tables == []

    def test_create_stage_has_no_source(self):
        """CREATE STAGE: object name not scooped into sources (AC-B2)."""
        parsed, node = _parse("CREATE STAGE ma.msstg_ingest URL='s3://x'")
        assert node is not None
        assert _source_ids(node) == []
        assert "ma.msstg_ingest" not in _ref_ids(parsed)

    def test_create_file_format_has_no_source(self):
        """CREATE FILE FORMAT: object name not scooped into sources (AC-B2)."""
        parsed, node = _parse("CREATE FILE FORMAT ma.msfmt_parquet TYPE=PARQUET")
        assert node is not None
        assert _source_ids(node) == []
        assert "ma.msfmt_parquet" not in _ref_ids(parsed)

    def test_create_schema_has_no_source(self):
        """CREATE SCHEMA: object name not scooped into sources."""
        parsed, node = _parse("CREATE SCHEMA da")
        assert node is not None
        assert _source_ids(node) == []


class TestProcedurePhantomSuppressedButInnerDmlIntact:
    """CREATE PROCEDURE top-level name is gated; spliced inner DML keeps lineage."""

    def test_create_procedure_name_not_a_source(self):
        """The procedure's own name is not emitted as a phantom source."""
        sql = "CREATE PROCEDURE da.load_p() RETURNS STRING LANGUAGE SQL AS $$ SELECT 1 $$"
        parsed, node = _parse(sql)
        assert node is not None
        assert "da.load_p" not in _source_ids(node)
        assert "da.load_p" not in _ref_ids(parsed)

    def test_spliced_inner_dml_lineage_intact(self):
        """Inner DML recovered/spliced from EXECUTE IMMEDIATE still has real sources.

        Real procedure lineage comes from the spliced inner statement (a separate
        statement node), which the source-gate never touches. The gate only
        suppresses the wrapper CREATE's own name. A recoverable inner CTAS keeps
        its source (`da.real_src`) — proving gate edge-neutrality for the
        inner-DML path.
        """
        from sqlcg.parsers.snowflake_parser import SnowflakeParser

        sql = "EXECUTE IMMEDIATE 'CREATE TABLE da.sink AS SELECT a FROM da.real_src';"
        parser = SnowflakeParser(SchemaResolver(dialect="snowflake"))
        parsed = parser.parse_file(Path("p.sql"), sql)
        refs = _ref_ids(parsed)
        # The spliced inner CTAS's real source survives the gate.
        assert "da.real_src" in refs
        assert "da.sink" in [d.full_id for d in parsed.defined_tables]


class TestTableAndViewCreateSourcesPreserved:
    """Control: legitimate TABLE/VIEW CREATE keep their real sources."""

    def test_ctas_keeps_real_source(self):
        """CREATE TABLE t AS SELECT ... FROM real_src keeps the source (AC-B4)."""
        parsed, node = _parse("CREATE TABLE da.t AS SELECT a FROM da.real_src")
        assert node is not None
        assert "da.real_src" in _source_ids(node)
        assert "da.real_src" in _ref_ids(parsed)
        # Target is not scooped as a source.
        assert "da.t" not in _source_ids(node)

    def test_create_view_keeps_real_source(self):
        """CREATE VIEW v AS SELECT ... FROM real_src keeps the source (AC-B4)."""
        parsed, node = _parse("CREATE OR REPLACE VIEW da.v AS SELECT a FROM da.real_src")
        assert node is not None
        assert "da.real_src" in _source_ids(node)
        assert "da.real_src" in _ref_ids(parsed)


class TestGateKeyedOnAst:
    """The gate keys off the sqlglot AST kind, independent of _classify."""

    def test_create_kinds_observed(self):
        """Confirm the AST kinds the gate relies on (documentation guard)."""
        kinds = {
            sql: sqlglot.parse_one(sql, dialect="snowflake").kind
            for sql in (
                "CREATE SEQUENCE ba.s",
                "CREATE STAGE ma.stg",
                "CREATE FILE FORMAT ff TYPE=CSV",
                "CREATE TABLE da.t AS SELECT a FROM da.s",
                "CREATE OR REPLACE VIEW da.v AS SELECT a FROM da.s",
            )
        }
        assert kinds["CREATE SEQUENCE ba.s"] == "SEQUENCE"
        assert kinds["CREATE TABLE da.t AS SELECT a FROM da.s"] == "TABLE"
        assert kinds["CREATE OR REPLACE VIEW da.v AS SELECT a FROM da.s"] == "VIEW"
