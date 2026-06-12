"""Integration tests for per-file TEMPORARY-table namespacing (kind='temp').

Indexes fixture files into an in-memory DuckDB graph and verifies all acceptance
criteria from plan/sprints/temp_table_namespacing.md.  Also contains regression
guards for fix/temp-namespacing-dual-write (v1.21.1) — schema-alias interaction.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sqlcg.cli.coverage import collect_coverage
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _routed(backend: DuckDBBackend):
    """Return a run_read_routed side-effect that routes queries to the given backend."""

    def _fn(query: str, params: dict):
        return backend.run_read(query, params)

    return _fn


@pytest.fixture
def db():
    """Fresh in-memory DuckDB backend."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Fixture SQL corpuses
# ---------------------------------------------------------------------------

# Corpus 1: two files each creating ba.tmp_base (the classic fusion scenario)
_FILE_A_SQL = """\
CREATE TABLE ba.real_src (x INT);
CREATE OR REPLACE TEMPORARY TABLE ba.tmp_base AS SELECT x FROM ba.real_src;
INSERT INTO ba.final_a (x) SELECT x FROM ba.tmp_base;
"""

_FILE_B_SQL = """\
CREATE TABLE ba.other_src (x INT);
CREATE OR REPLACE TEMPORARY TABLE ba.tmp_base AS SELECT x FROM ba.other_src;
INSERT INTO ba.final_b (x) SELECT x FROM ba.tmp_base;
"""

# Corpus 2: permanent multi-writer table (must remain a single node, unchanged).
# Two files each INSERT from ba.shared_table into different targets.
# No TEMPORARY property → must stay kind='table' (single fused node).
_FILE_C_SQL = "INSERT INTO ba.out_c (y) SELECT y FROM ba.shared_table;"
_FILE_D_SQL = "INSERT INTO ba.out_d (y) SELECT y FROM ba.shared_table;"

# Corpus 3: real_src → ba.tmp_base → real_final (chain through a temp)
_CHAIN_SQL = """\
CREATE TABLE ba.chain_src (col INT);
CREATE OR REPLACE TEMPORARY TABLE ba.tmp_chain AS SELECT col FROM ba.chain_src;
INSERT INTO ba.chain_final (col) SELECT col FROM ba.tmp_chain;
"""


# ---------------------------------------------------------------------------
# Test class: two-file corpus (de-fusion acceptance)
# ---------------------------------------------------------------------------


@pytest.fixture
def two_file_corpus(db, tmp_path):
    """Index file A and file B (both define ba.tmp_base) into one in-memory DB."""
    (tmp_path / "file_a.sql").write_text(_FILE_A_SQL)
    (tmp_path / "file_b.sql").write_text(_FILE_B_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db


class TestTwoFileTempDefusion:
    """Two files each defining ba.tmp_base produce TWO distinct kind='temp' nodes.

    Guards temp_table_namespacing.md Acceptance — distinct keys, no fusion.
    """

    def test_two_temp_nodes_with_distinct_keys(self, two_file_corpus):
        """Two kind='temp' SqlTable nodes exist, each keyed to its defining file.

        Guards temp_table_namespacing.md Acceptance criterion.
        """
        rows = two_file_corpus.run_read(
            "SELECT qualified, kind FROM \"SqlTable\" WHERE kind = 'temp' AND name = 'tmp_base'",
            {},
        )
        keys = [r["qualified"] for r in rows]
        assert len(keys) == 2, (
            f"Expected exactly 2 kind='temp' nodes for ba.tmp_base, got {len(keys)}: {keys}"
        )
        assert keys[0] != keys[1], "The two temp nodes must have distinct qualified keys"
        assert any("file_a.sql" in k for k in keys), "file_a.sql key not found"
        assert any("file_b.sql" in k for k in keys), "file_b.sql key not found"

    def test_no_shared_bare_tmp_base_table_node(self, two_file_corpus):
        """There is no bare ba.tmp_base kind='table' node (the pre-fix fusion artifact).

        Guards temp_table_namespacing.md Acceptance.
        """
        rows = two_file_corpus.run_read(
            "SELECT qualified, kind FROM \"SqlTable\" WHERE qualified = 'ba.tmp_base'",
            {},
        )
        assert rows == [], (
            f"Bare ba.tmp_base kind='table' node must not exist after namespacing: {rows}"
        )

    def test_permanent_shared_table_stays_single_node(self, db, tmp_path):
        """Genuinely shared physical table (two writers, no TEMPORARY) stays one kind='table' node.

        Guards temp_table_namespacing.md §Non-Goals (368−85 set untouched).
        """
        (tmp_path / "c.sql").write_text(_FILE_C_SQL)
        (tmp_path / "d.sql").write_text(_FILE_D_SQL)
        Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)

        rows = db.run_read(
            "SELECT qualified, kind FROM \"SqlTable\" WHERE name = 'shared_table'",
            {},
        )
        table_rows = [r for r in rows if r["kind"] == "table"]
        assert len(table_rows) == 1, (
            f"Permanent shared table must remain a single kind='table' node: {table_rows}"
        )
        assert table_rows[0]["qualified"] == "ba.shared_table"


# ---------------------------------------------------------------------------
# Test class: lineage chain through temp (user-flagged correctness gate)
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_corpus(db, tmp_path):
    """Index the chain fixture: real_src → ba.tmp_chain → chain_final."""
    (tmp_path / "chain.sql").write_text(_CHAIN_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db


class TestLineageChainThroughTemp:
    """Intra-file lineage chain through a temp is re-keyed, not severed.

    Guards temp_table_namespacing.md Acceptance — chain_src → tmp → chain_final end-to-end.
    """

    def test_column_lineage_edges_span_the_temp(self, chain_corpus):
        """COLUMN_LINEAGE edges exist on both sides of the temp node.

        Guards temp_table_namespacing.md §Lineage-preservation invariant.
        """
        # Edges into the temp (chain_src → tmp_chain)
        _q_into = (
            "SELECT src_key, dst_key FROM \"COLUMN_LINEAGE\" WHERE dst_key LIKE '%::ba.tmp_chain.%'"
        )
        into_temp = chain_corpus.run_read(_q_into, {})
        # Edges out of the temp (tmp_chain → chain_final)
        _q_out = (
            "SELECT src_key, dst_key FROM \"COLUMN_LINEAGE\" WHERE src_key LIKE '%::ba.tmp_chain.%'"
        )
        out_of_temp = chain_corpus.run_read(_q_out, {})
        assert into_temp, (
            "No COLUMN_LINEAGE edges found INTO ba.tmp_chain — temp node is severed upstream"
        )
        assert out_of_temp, (
            "No COLUMN_LINEAGE edges found OUT OF ba.tmp_chain — temp node is severed downstream"
        )

    def test_temp_node_kind_is_temp(self, chain_corpus):
        """The tmp_chain node in the graph has kind='temp'.

        Guards temp_table_namespacing.md Step 3.1.
        """
        rows = chain_corpus.run_read(
            "SELECT qualified, kind FROM \"SqlTable\" WHERE name = 'tmp_chain'",
            {},
        )
        assert rows, "tmp_chain node not found in SqlTable"
        kinds = {r["kind"] for r in rows}
        assert "temp" in kinds, f"Expected kind='temp' for tmp_chain, got {kinds}"
        assert "table" not in kinds, f"tmp_chain must not have kind='table': {kinds}"

    def test_single_namespaced_identity_within_file(self, chain_corpus):
        """The temp's CREATE-target key and its same-file read key are byte-identical.

        Guards temp_table_namespacing.md §Lineage-preservation invariant.
        """
        # Find the temp node's qualified key
        rows = chain_corpus.run_read(
            "SELECT qualified FROM \"SqlTable\" WHERE name = 'tmp_chain' AND kind = 'temp'",
            {},
        )
        assert rows, "No temp node found for tmp_chain"
        temp_key = rows[0]["qualified"]
        assert "::" in temp_key, f"Temp key must be namespaced, got {temp_key!r}"

        # All COLUMN_LINEAGE edges referencing tmp_chain use the same namespaced key.
        # Use the temp node's own qualified key (which is the table part of the column key).
        all_edges = chain_corpus.run_read(
            'SELECT src_key, dst_key FROM "COLUMN_LINEAGE"',
            {},
        )
        tmp_chain_edges = [
            e for e in all_edges if "tmp_chain" in e["src_key"] or "tmp_chain" in e["dst_key"]
        ]
        for edge in tmp_chain_edges:
            if "tmp_chain" in edge["src_key"]:
                assert "::" in edge["src_key"], (
                    f"tmp_chain src_key not namespaced: {edge['src_key']!r}"
                )
            if "tmp_chain" in edge["dst_key"]:
                assert "::" in edge["dst_key"], (
                    f"tmp_chain dst_key not namespaced: {edge['dst_key']!r}"
                )


# ---------------------------------------------------------------------------
# Test class: cross-batch kind protection (Amendment 1, BLOCKER gate)
# ---------------------------------------------------------------------------


class TestCrossBatchKindProtection:
    """committed kind='temp' survives cross-batch reference/star-source rows.

    Guards temp_table_namespacing.md Step 3.2 (Amendment 1 — BLOCKER).
    """

    def test_temp_kind_survives_table_reference_row(self, db):
        """A committed kind='temp' node is NOT overwritten by a later kind='table' row.

        Guards temp_table_namespacing.md Step 3.2.
        """
        # Batch 1: commit a kind='temp' node
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "etl/x.sql::ba.tmp_x",
                    "name": "tmp_x",
                    "catalog": "",
                    "db": "ba",
                    "kind": "temp",
                    "defined_in_file": "etl/x.sql",
                }
            ],
        )
        # Batch 2: upsert a kind='table' reference row for the same key
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "etl/x.sql::ba.tmp_x",
                    "name": "tmp_x",
                    "catalog": "",
                    "db": "ba",
                    "kind": "table",
                    "defined_in_file": "",
                }
            ],
        )
        rows = db.run_read(
            "SELECT kind FROM \"SqlTable\" WHERE qualified = 'etl/x.sql::ba.tmp_x'",
            {},
        )
        assert rows, "Node must still exist"
        assert rows[0]["kind"] == "temp", (
            f"kind='temp' was overwritten to {rows[0]['kind']!r} by a kind='table' reference row"
        )

    def test_temp_kind_survives_derived_reference_row(self, db):
        """A committed kind='temp' node is NOT overwritten by a later kind='derived' row.

        Guards temp_table_namespacing.md Step 3.2.
        """
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "etl/x.sql::ba.tmp_y",
                    "name": "tmp_y",
                    "catalog": "",
                    "db": "ba",
                    "kind": "temp",
                    "defined_in_file": "etl/x.sql",
                }
            ],
        )
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "etl/x.sql::ba.tmp_y",
                    "name": "tmp_y",
                    "catalog": "",
                    "db": "ba",
                    "kind": "derived",
                    "defined_in_file": "",
                }
            ],
        )
        rows = db.run_read(
            "SELECT kind FROM \"SqlTable\" WHERE qualified = 'etl/x.sql::ba.tmp_y'",
            {},
        )
        assert rows[0]["kind"] == "temp", (
            f"kind='temp' was overwritten by kind='derived': {rows[0]['kind']!r}"
        )

    def test_existing_guard_table_vs_derived_still_holds(self, db):
        """Regression: existing kind='table'/'view' vs kind='derived' guard is unchanged.

        Guards temp_table_namespacing.md Step 3.2 (counterpart).
        """
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "ba.real_table",
                    "name": "real_table",
                    "catalog": "",
                    "db": "ba",
                    "kind": "table",
                    "defined_in_file": "etl/x.sql",
                }
            ],
        )
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "ba.real_table",
                    "name": "real_table",
                    "catalog": "",
                    "db": "ba",
                    "kind": "derived",
                    "defined_in_file": "",
                }
            ],
        )
        rows = db.run_read(
            "SELECT kind FROM \"SqlTable\" WHERE qualified = 'ba.real_table'",
            {},
        )
        assert rows[0]["kind"] == "table", (
            f"kind='table' should survive a kind='derived' update: {rows[0]['kind']!r}"
        )

    def test_star_source_row_does_not_re_fuse_temp(self, db):
        """A star_sources row (hardcoded kind='table') for a temp key leaves kind='temp' intact.

        This is the Warning 3 path: the indexer emits kind='table' for star_sources.
        The Amendment 1 ON CONFLICT guard prevents re-fusion.
        Guards temp_table_namespacing.md §Consumer audit — star_sources.
        """
        # Commit the temp node first
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "etl/s.sql::ba.tmp_star",
                    "name": "tmp_star",
                    "catalog": "",
                    "db": "ba",
                    "kind": "temp",
                    "defined_in_file": "etl/s.sql",
                }
            ],
        )
        # Simulate the star_sources emit: hardcoded kind='table'
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "etl/s.sql::ba.tmp_star",
                    "name": "tmp_star",
                    "catalog": "",
                    "db": "ba",
                    "kind": "table",
                    "defined_in_file": "",
                }
            ],
        )
        rows = db.run_read(
            "SELECT kind FROM \"SqlTable\" WHERE qualified = 'etl/s.sql::ba.tmp_star'",
            {},
        )
        assert rows[0]["kind"] == "temp", (
            f"Star-source row (kind='table') re-fused the temp node: {rows[0]['kind']!r}"
        )


# ---------------------------------------------------------------------------
# Test class: consumer round-trips
# ---------------------------------------------------------------------------


class TestConsumerRoundTrips:
    """Consumer behaviour for kind='temp' nodes.

    Guards temp_table_namespacing.md §Consumer audit.
    """

    def test_temp_excluded_from_harvest_usage_catalog(self, two_file_corpus):
        """Namespaced temp keys are excluded from _harvest_usage_catalog (HAS_COLUMN usage).

        Guards temp_table_namespacing.md §Consumer audit — _harvest_usage_catalog.
        """
        # Check that no HAS_COLUMN usage rows exist for ::ba.tmp_base column keys
        rows = two_file_corpus.run_read(
            'SELECT src_key, dst_key FROM "HAS_COLUMN" '
            "WHERE source = 'usage' AND src_key LIKE '%::ba.tmp_base'",
            {},
        )
        assert rows == [], f"Namespaced temp keys must not appear in usage HAS_COLUMN: {rows}"

    def test_temp_not_upgraded_by_catalog_kind_upgrade(self, db):
        """upgrade_derived_to_table_for_keys must NOT touch kind='temp' nodes.

        Guards temp_table_namespacing.md §Consumer audit — catalog kind-upgrade.
        """
        db.upsert_nodes_bulk(
            "SqlTable",
            [
                {
                    "qualified": "etl/x.sql::ba.tmp_no_upgrade",
                    "name": "tmp_no_upgrade",
                    "catalog": "",
                    "db": "ba",
                    "kind": "temp",
                    "defined_in_file": "etl/x.sql",
                }
            ],
        )
        # Simulate catalog kind upgrade call for the temp's qualified key
        db.upgrade_derived_to_table_for_keys(["etl/x.sql::ba.tmp_no_upgrade"])

        rows = db.run_read(
            "SELECT kind FROM \"SqlTable\" WHERE qualified = 'etl/x.sql::ba.tmp_no_upgrade'",
            {},
        )
        assert rows[0]["kind"] == "temp", (
            f"upgrade_derived_to_table_for_keys must not upgrade kind='temp', "
            f"but got kind={rows[0]['kind']!r}"
        )

    def test_temp_not_counted_as_cte_collision(self, two_file_corpus):
        """Namespaced temp nodes do not register in _Q_CTE_COLLISIONS.

        Guards temp_table_namespacing.md §Consumer audit — _Q_CTE_COLLISIONS.
        """
        with patch("sqlcg.cli.coverage.run_read_routed", side_effect=_routed(two_file_corpus)):
            stats = collect_coverage()
        # cte_key_collisions counts distinct CTE/derived dst column keys — temp must be 0
        assert stats.cte_key_collisions == 0, (
            f"Temp nodes must not count as CTE collisions; got {stats.cte_key_collisions}"
        )


# ---------------------------------------------------------------------------
# Test class: scoped-health coverage
# ---------------------------------------------------------------------------


class TestCoverageScoped:
    """Scoped-health denominator excludes kind='temp' dst keys.

    Guards temp_table_namespacing.md Step 4.1.
    """

    def test_scoped_health_excludes_temp(self, two_file_corpus):
        """Temp endpoints do not inflate total_edges_scoped.

        Guards temp_table_namespacing.md Step 4.1.
        """
        with patch("sqlcg.cli.coverage.run_read_routed", side_effect=_routed(two_file_corpus)):
            stats = collect_coverage()
        # total_edges_scoped must be <= total_edges (temp-dst edges excluded)
        assert stats.total_edges_scoped <= stats.total_edges, (
            "total_edges_scoped must not exceed total_edges "
            "(temp edges should move to the scoped-excluded bucket)"
        )

    def test_coverage_label_includes_temp(self, two_file_corpus):
        """Rendered scoped-health label reads 'excl. CTE/derived/temp'.

        Guards temp_table_namespacing.md Step 4.1.
        """
        from sqlcg.cli.coverage import render_coverage_lines

        with patch("sqlcg.cli.coverage.run_read_routed", side_effect=_routed(two_file_corpus)):
            stats = collect_coverage()
        lines = render_coverage_lines(stats)
        scoped_line = next(
            (line for line in lines if "scoped" in line.lower() and "excl." in line.lower()),
            None,
        )
        assert scoped_line is not None, "No scoped-health line found in rendered output"
        assert "temp" in scoped_line.lower(), (
            f"Rendered scoped line does not mention 'temp': {scoped_line!r}"
        )


# ---------------------------------------------------------------------------
# v1.21.1 regression guard: schema-alias + USE SCHEMA dual-write
# ---------------------------------------------------------------------------

# Fixture modelled on the real wtfv_bon.sql DWH pattern:
#   USE SCHEMA BA_TMP (schema alias ba_tmp → ba)
#   CREATE TEMPORARY TABLE tmp_base AS SELECT ... FROM da.real_src
#   INSERT INTO real_target SELECT ... FROM tmp_base
#
# Pre-fix (v1.21.0): _lineage_node_to_edges saw src_db='ba_tmp' (pre-alias) from
# the raw exp.Table, computed _temp_identity('ba_tmp','tmp_base')='ba_tmp.tmp_base',
# which missed _current_file_temp_keys {'ba.tmp_base'} → fell through →
# _apply_table_alias produced TableRef(db='ba', role='table') →
# dual write: BOTH '<rel>::ba.tmp_base' (kind='temp') AND 'ba.tmp_base' (kind='table').
#
# Post-fix (v1.21.1): alias is applied to src_db before _temp_identity → match →
# returns namespaced role='temp' TableRef → single identity, no duplicate.

_WTFV_BON_STYLE_SQL = """\
USE SCHEMA BA_TMP;

CREATE OR REPLACE TEMPORARY TABLE tmp_base AS
SELECT winkelnr, bontype
FROM DA.real_src
WHERE status = 2;

INSERT INTO real_target
SELECT base.winkelnr, base.bontype
FROM tmp_base base
INNER JOIN BA.ref_table r ON base.winkelnr = r.nr;
"""


class TestSchemaAliasDualWriteRegression:
    """The USE SCHEMA + schema-alias + bare-temp pattern must produce a single
    namespaced identity with no residual un-namespaced kind='table' node.

    Reproduces the live-DWH dual-write from v1.21.0.
    Guards fix/temp-namespacing-dual-write (v1.21.1).
    """

    def test_no_unnamespaced_tmp_base_table_node_with_alias(self, tmp_path):
        """After indexing a file with USE SCHEMA BA_TMP (alias ba_tmp→ba) and a bare
        CREATE TEMPORARY TABLE tmp_base, the un-namespaced 'ba.tmp_base' kind='table'
        node must NOT exist.

        Pre-fix: both 'ba.tmp_base' (kind='table') AND '<rel>::ba.tmp_base' (kind='temp')
        existed — the dual-write.  Post-fix: only the namespaced kind='temp' node.

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        rel = "etl/sql/fact/wtfv_bon_style.sql"
        (tmp_path / "etl" / "sql" / "fact").mkdir(parents=True)
        (tmp_path / rel).write_text(_WTFV_BON_STYLE_SQL)

        # Create a .sqlcg.toml with the schema alias so index_repo picks it up
        sqlcg_toml = tmp_path / ".sqlcg.toml"
        sqlcg_toml.write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')

        backend = DuckDBBackend(":memory:")
        backend.init_schema()
        try:
            Indexer().index_repo(tmp_path, dialect="snowflake", db=backend, use_git=False)

            # The un-namespaced 'ba.tmp_base' kind='table' node must NOT exist
            rows = backend.run_read(
                "SELECT qualified, kind FROM \"SqlTable\" WHERE qualified = 'ba.tmp_base'",
                {},
            )
            assert rows == [], (
                f"Un-namespaced 'ba.tmp_base' kind='table' node must not exist after "
                f"indexing with schema alias ba_tmp→ba.  This is the v1.21.0 dual-write "
                f"(fix/temp-namespacing-dual-write). Found: {rows}"
            )
        finally:
            backend.close()

    def test_namespaced_temp_node_exists_with_alias(self, tmp_path):
        """After indexing with USE SCHEMA BA_TMP (alias ba_tmp→ba), the namespaced
        kind='temp' node '<rel>::ba.tmp_base' must exist and be the ONLY tmp_base node.

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        rel = "etl/sql/fact/wtfv_bon_style.sql"
        (tmp_path / "etl" / "sql" / "fact").mkdir(parents=True)
        (tmp_path / rel).write_text(_WTFV_BON_STYLE_SQL)

        sqlcg_toml = tmp_path / ".sqlcg.toml"
        sqlcg_toml.write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')

        backend = DuckDBBackend(":memory:")
        backend.init_schema()
        try:
            Indexer().index_repo(tmp_path, dialect="snowflake", db=backend, use_git=False)

            # Exactly one tmp_base node — the namespaced kind='temp'
            rows = backend.run_read(
                "SELECT qualified, kind FROM \"SqlTable\" WHERE name = 'tmp_base'",
                {},
            )
            assert len(rows) == 1, (
                f"Expected exactly 1 tmp_base node (the namespaced kind='temp'), "
                f"got {len(rows)}: {rows}"
            )
            assert rows[0]["kind"] == "temp", (
                f"The single tmp_base node must have kind='temp', got {rows[0]['kind']!r}"
            )
            assert "::" in rows[0]["qualified"], (
                f"The tmp_base node must be namespaced, got {rows[0]['qualified']!r}"
            )
        finally:
            backend.close()

    def test_lineage_chain_through_temp_with_alias(self, tmp_path):
        """With schema alias ba_tmp→ba, COLUMN_LINEAGE edges flow through the namespaced
        temp from da.real_src into real_target — the chain is intact end-to-end.

        Pre-fix: edges from INSERT used 'ba.tmp_base' (un-namespaced) as src, so the
        namespaced node had zero outgoing edges (orphaned sink).
        Post-fix: edges use '<rel>::ba.tmp_base' → both sides of the temp are wired.

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        rel = "etl/sql/fact/wtfv_bon_style.sql"
        (tmp_path / "etl" / "sql" / "fact").mkdir(parents=True)
        (tmp_path / rel).write_text(_WTFV_BON_STYLE_SQL)

        sqlcg_toml = tmp_path / ".sqlcg.toml"
        sqlcg_toml.write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')

        backend = DuckDBBackend(":memory:")
        backend.init_schema()
        try:
            Indexer().index_repo(tmp_path, dialect="snowflake", db=backend, use_git=False)

            # Find the namespaced temp node's qualified key
            temp_rows = backend.run_read(
                "SELECT qualified FROM \"SqlTable\" WHERE name = 'tmp_base' AND kind = 'temp'",
                {},
            )
            assert temp_rows, "No namespaced kind='temp' node found for tmp_base"
            temp_key = temp_rows[0]["qualified"]

            # Edges INTO the temp (from da.real_src → tmp_base)
            # Edge key format: "<table_qualified>.<col_name>"
            into_temp = backend.run_read(
                'SELECT src_key, dst_key FROM "COLUMN_LINEAGE" WHERE dst_key LIKE ?',
                {"prefix": f"{temp_key}.%"},
            )
            # Edges OUT OF the temp (from tmp_base → real_target)
            out_of_temp = backend.run_read(
                'SELECT src_key, dst_key FROM "COLUMN_LINEAGE" WHERE src_key LIKE ?',
                {"prefix": f"{temp_key}.%"},
            )

            assert into_temp, (
                f"No COLUMN_LINEAGE edges INTO namespaced temp '{temp_key}' — "
                f"pre-fix: CREATE stmt edges were correct; post-fix they still are. "
                f"Check that the chain_src→tmp edge was not broken."
            )
            assert out_of_temp, (
                f"No COLUMN_LINEAGE edges OUT OF namespaced temp '{temp_key}' — "
                f"pre-fix: INSERT stmt edges pointed at un-namespaced 'ba.tmp_base' "
                f"(orphaned sink); post-fix they must use the namespaced key. "
                f"This is the core correctness check for fix/temp-namespacing-dual-write."
            )
        finally:
            backend.close()


# ---------------------------------------------------------------------------
# v1.21.1 regression guard: BEGIN-in-comment false-positive scripting detection
# ---------------------------------------------------------------------------

# Fixture modelled on the real wtfv_cyclische_telling.sql DWH pattern:
#   - SQL comment contains the word "begin" (not a scripting keyword)
#   - USE SCHEMA BA_TMP + schema alias ba_tmp → ba
#   - CREATE TEMPORARY TABLE tmp_base AS SELECT ... UNION SELECT ...
#   - INSERT INTO real_target SELECT ... FROM tmp_base
#
# Pre-fix: Tokenizer.from_dialect("snowflake") raises AttributeError (method does
# not exist in sqlglot 30.x) → except-clause falls back to re.search(r'\bBEGIN\b')
# → matches "begin" in the SQL comment → _has_scripting_block returns True →
# _parse_scripting_file runs → _EMBEDDED_DML only matches DML keywords (not CREATE)
# → _current_file_temp_keys never populated → INSERT emits un-namespaced
# 'ba.tmp_base' as src_key, creating a shared kind='table' node that bridges ETL files.
#
# Post-fix: Tokenizer(dialect="snowflake") succeeds → token-aware scan → no BEGIN
# token → _has_scripting_block returns False → AnsiParser path → full temp registration.

_BEGIN_IN_COMMENT_SQL = """\
-- de pvcode wordt aan het begin van een telcyclus vastgelegd
use schema BA_TMP;

create or replace temporary table tmp_base as
select winkelnr, bontype
from DA.real_src
where status = 2
union
select winkelnr, bontype
from DA.alt_src
where status = 3;

insert into real_target
select base.winkelnr, base.bontype
from tmp_base base;
"""


class TestBeginInCommentDualWriteRegression:
    """A file with BEGIN only in a SQL comment must NOT be routed through the
    scripting fallback path, and must produce a single namespaced temp identity.

    Root cause of the dual-write observed on the live DWH after the v1.21.1
    read-side fix: Tokenizer.from_dialect() does not exist → except-clause
    → regex matches 'begin' in comments → false scripting detection.

    Guards fix/temp-namespacing-dual-write (v1.21.1).
    (plan/sprints/temp_table_namespacing.md §Deviations).
    """

    def test_no_bare_tmp_base_node_when_begin_in_comment(self, tmp_path):
        """After indexing a file where BEGIN appears only in a SQL comment, the bare
        'ba.tmp_base' kind='table' node must NOT exist.

        Pre-fix: Tokenizer.from_dialect() → except → regex → 'begin' matched in comment
        → scripting path → CREATE TEMPORARY skipped → INSERT emits bare src node.
        Post-fix: correct Tokenizer API → no BEGIN token → FULL parse → namespaced temp.

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        rel = "etl/sql/fact/begin_comment.sql"
        (tmp_path / "etl" / "sql" / "fact").mkdir(parents=True)
        (tmp_path / rel).write_text(_BEGIN_IN_COMMENT_SQL)

        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')

        backend = DuckDBBackend(":memory:")
        backend.init_schema()
        try:
            Indexer().index_repo(tmp_path, dialect="snowflake", db=backend, use_git=False)

            # No bare 'ba.tmp_base' node must exist
            bare_rows = backend.run_read(
                "SELECT qualified, kind FROM \"SqlTable\" WHERE qualified = 'ba.tmp_base'",
                {},
            )
            assert bare_rows == [], (
                f"Un-namespaced 'ba.tmp_base' kind='table' node must not exist when BEGIN "
                f"appears only in a SQL comment.  Pre-fix: Tokenizer.from_dialect() raises "
                f"→ regex false-positive → scripting path → _current_file_temp_keys never "
                f"populated → INSERT emits bare src, creating this node.  "
                f"Post-fix: Tokenizer(dialect=...) fix → FULL parse → namespaced temp only.  "
                f"Found: {bare_rows}"
            )
        finally:
            backend.close()

    def test_single_namespaced_tmp_base_when_begin_in_comment(self, tmp_path):
        """After indexing with BEGIN in comment + schema_aliases, exactly ONE tmp_base
        node exists — the namespaced kind='temp' node.

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        rel = "etl/sql/fact/begin_comment.sql"
        (tmp_path / "etl" / "sql" / "fact").mkdir(parents=True)
        (tmp_path / rel).write_text(_BEGIN_IN_COMMENT_SQL)

        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')

        backend = DuckDBBackend(":memory:")
        backend.init_schema()
        try:
            Indexer().index_repo(tmp_path, dialect="snowflake", db=backend, use_git=False)

            rows = backend.run_read(
                "SELECT qualified, kind FROM \"SqlTable\" WHERE name = 'tmp_base'",
                {},
            )
            assert len(rows) == 1, (
                f"Expected exactly 1 tmp_base node (the namespaced kind='temp'), "
                f"got {len(rows)}: {rows}"
            )
            assert rows[0]["kind"] == "temp", (
                f"The single tmp_base node must have kind='temp', got {rows[0]['kind']!r}"
            )
            assert "::" in rows[0]["qualified"], (
                f"The tmp_base node must be namespaced, got {rows[0]['qualified']!r}"
            )
        finally:
            backend.close()

    def test_no_bare_column_lineage_edges_when_begin_in_comment(self, tmp_path):
        """After indexing with BEGIN in comment + schema_aliases, COLUMN_LINEAGE has
        zero rows with bare 'ba.tmp_base' as either src_key or dst_key.

        This is the core assertion for the shepherd-observed dual-write: both
        INSERT src edges AND CREATE dst edges must be namespaced, not bare.

        Guards fix/temp-namespacing-dual-write (v1.21.1).
        """
        rel = "etl/sql/fact/begin_comment.sql"
        (tmp_path / "etl" / "sql" / "fact").mkdir(parents=True)
        (tmp_path / rel).write_text(_BEGIN_IN_COMMENT_SQL)

        (tmp_path / ".sqlcg.toml").write_text('[sqlcg.schema_aliases]\nba_tmp = "ba"\n')

        backend = DuckDBBackend(":memory:")
        backend.init_schema()
        try:
            Indexer().index_repo(tmp_path, dialect="snowflake", db=backend, use_git=False)

            bare_dst = backend.run_read(
                'SELECT src_key, dst_key FROM "COLUMN_LINEAGE" WHERE dst_key LIKE ?',
                {"prefix": "ba.tmp_base.%"},
            )
            bare_src = backend.run_read(
                'SELECT src_key, dst_key FROM "COLUMN_LINEAGE" WHERE src_key LIKE ?',
                {"prefix": "ba.tmp_base.%"},
            )

            assert bare_dst == [], (
                f"Zero COLUMN_LINEAGE rows with bare 'ba.tmp_base' dst_key expected.  "
                f"Pre-fix: CREATE stmt from scripting path never emitted (so this was 0), "
                f"but the INSERT stmt (correctly extracted) emitted bare src.  "
                f"Post-fix: both CREATE and INSERT are on the FULL parse path → namespaced.  "
                f"Found dst: {bare_dst}"
            )
            assert bare_src == [], (
                f"Zero COLUMN_LINEAGE rows with bare 'ba.tmp_base' src_key expected.  "
                f"Pre-fix: INSERT emitted bare 'ba.tmp_base' as src because "
                f"_current_file_temp_keys was empty (CREATE was never extracted).  "
                f"Post-fix: FULL parse → temp keys registered → namespaced src.  "
                f"Found src: {bare_src}"
            )
        finally:
            backend.close()
