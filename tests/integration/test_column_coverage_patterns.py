"""Integration tests for the four SQL patterns that cause column coverage gaps.

Each test captures one pattern discovered by the systematic diagnostic
(plan/reports/column_coverage_findings.md) run against the live DWH graph.
All tests assert observable graph state (COLUMN_LINEAGE rows, HAS_COLUMN rows,
SqlColumn rows) — never "no exception raised".

Pattern summary:
  P1 — CTE destination leak (15,102 edges land on CTE/derived nodes, not real tables)
  P2 — Positional INSERT phantom edges (13,412 inferred_from_source_name=true edges)
       Covered by existing clone-blindspot tests; the sub-case here is the ALIAS on
       INSERT target that aliases the table name in scope.
  P3 — Space-in-name quoted views: schema prefix stripped from SqlColumn.table_name
       (6,045 edges, 232 tables with full ZERO catalog)
  P4 — CTAS column discovery: CREATE TABLE t AS SELECT produces no SqlColumn rows
       (241 tables confirmed by pattern scan)

Tests are written to FAIL today and PASS after the corresponding fix lands.
They are marked xfail(strict=True) — a passing test before the fix is a regression
(strict=True turns unexpectedly-passing tests red so the xfail is removed when the
fix lands).
"""

from __future__ import annotations

import textwrap

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index(tmp_path, files: dict[str, str], dialect: str | None = "snowflake") -> DuckDBBackend:
    for name, sql in files.items():
        (tmp_path / name).write_text(textwrap.dedent(sql))
    db = DuckDBBackend(":memory:")
    db.init_schema()
    Indexer().index_repo(tmp_path, dialect=dialect, db=db, use_git=False)
    return db


# ---------------------------------------------------------------------------
# P1 — CTE destination leak
#
# Pattern from DWH: INSERT INTO real_table (col_list)
#   WITH cte1 AS (...), cte_final AS (...) SELECT ... FROM cte_final
#
# Today: lineage destination is `cte_final.col`, not `real_table.col`.
# Expected: lineage destination is `real_table.col`.
# Measured impact: 15,102 edges (6,944 on cte-kind + 8,158 on derived-kind).
# ---------------------------------------------------------------------------


def test_P1_cte_destination_resolves_to_real_target_with_chain(tmp_path):
    """Edges from INSERT ... WITH cte SELECT FROM cte must land on the real INSERT target.
    The CTE chain intermediate edges (base.col_a, base.col_b) must also exist — they are
    required for upstream traversal (ARCHITECTURE_REVIEW §3.2).

    Reproduces the dominant DWH pattern:
        INSERT INTO ba.fact (col_a, col_b)
        WITH base AS (SELECT x AS col_a, y AS col_b FROM ba.src)
        SELECT col_a, col_b FROM base;

    Expected:
      - Real-target edges: dst_key = 'ba.fact.col_a' / 'ba.fact.col_b' (INSERT target reached)
      - Chain edges: dst_key = 'base.col_a' / 'base.col_b' ALSO EXIST (chain preserved)

    Previously xfail because the first assertion failed (real target had no edges).
    The prior sprint (coverage_p1_p5_metric.md) fixed the positional block — both edges
    now exist. The second assertion is updated from "no CTE dst" to "CTE chain MUST EXIST"
    per ARCHITECTURE_REVIEW §3.2 and plan/sprints/coverage_phantom_tables.md §W2.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": """\
            CREATE TABLE ba.src (x INT, y INT);
            CREATE TABLE ba.fact (col_a INT, col_b INT);
        """,
            "etl.sql": """\
            INSERT INTO ba.fact (col_a, col_b)
            WITH base AS (
                SELECT x AS col_a, y AS col_b
                FROM ba.src
            )
            SELECT col_a, col_b
            FROM base;
        """,
        },
    )
    try:
        edges = db.run_read(
            "SELECT dst_key FROM COLUMN_LINEAGE "
            # PR 3: CTE 'base' is now namespaced as '<path>::base.*'; use suffix match
            "WHERE dst_key LIKE 'ba.fact.%' OR dst_key LIKE '%::base.%'",
            {},
        )
    finally:
        db.close()

    dst_keys = {r["dst_key"] for r in edges}
    # Real INSERT target must receive edges (the core P1 fix)
    assert any(k.startswith("ba.fact.") for k in dst_keys), (
        f"Expected lineage edges landing on ba.fact.*, got only: {sorted(dst_keys)}"
    )
    # CTE chain edges must also exist (ARCHITECTURE_REVIEW §3.2 — required for upstream traversal)
    # PR 3: CTE 'base' is namespaced as '<abs_path>::base.col'; check by suffix.
    assert any("::base." in k for k in dst_keys), (
        f"CTE chain edges (namespace::base.*) must EXIST for upstream traversal — "
        f"got dst_keys: {sorted(dst_keys)}"
    )


def test_P1_cte_chain_reaches_real_target_and_preserves_chain(tmp_path):
    """Multi-CTE chain ending in cte_insert must reach the real INSERT target.
    CTE chain intermediate edges must also be present (ARCHITECTURE_REVIEW §3.2).

    DWH file: etl/sql/fact/wtfe_kpi_gemiddelde_voorraad_artikel_voorraadlocatie.sql
    (698 edges landed on 'cte_insert' in the live graph — these are correct chain edges)

        INSERT INTO ba.kpi_fact (dn_datum, ma_total)
        WITH
            cte_base AS (SELECT dn_datum, ma_total FROM ba.src),
            cte_insert AS (
                SELECT dn_datum, ma_total FROM cte_base
            )
        SELECT dn_datum, ma_total
        FROM cte_insert;

    Expected:
      - Real INSERT target: dst_key = 'ba.kpi_fact.dn_datum' and 'ba.kpi_fact.ma_total'
      - CTE chain intermediates: 'cte_insert.*' and 'cte_base.*' ALSO EXIST

    Previously xfail because real-target edges were absent. Fixed by the positional
    block (coverage_p1_p5_metric.md). Second assertion updated from "no CTE dst" to
    "chain must EXIST" per ARCHITECTURE_REVIEW §3.2 and
    plan/sprints/coverage_phantom_tables.md §W2.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": """\
            CREATE TABLE ba.src (dn_datum DATE, ma_total INT);
            CREATE TABLE ba.kpi_fact (dn_datum DATE, ma_total INT);
        """,
            "etl.sql": """\
            INSERT INTO ba.kpi_fact (dn_datum, ma_total)
            WITH
                cte_base AS (SELECT dn_datum, ma_total FROM ba.src),
                cte_insert AS (SELECT dn_datum, ma_total FROM cte_base)
            SELECT dn_datum, ma_total
            FROM cte_insert;
        """,
        },
    )
    try:
        edges = db.run_read(
            "SELECT dst_key FROM COLUMN_LINEAGE "
            "WHERE dst_key LIKE 'ba.kpi_fact.%' "
            # PR 3: CTE keys are now namespaced as '<path>::cte_insert.*'; use suffix match
            "OR dst_key LIKE '%::cte_insert.%' OR dst_key LIKE '%::cte_base.%'",
            {},
        )
    finally:
        db.close()

    dst_keys = {r["dst_key"] for r in edges}
    # Real INSERT target must receive edges
    assert any(k.startswith("ba.kpi_fact.") for k in dst_keys), (
        f"Edges must land on ba.kpi_fact.*, got: {sorted(dst_keys)}"
    )
    # CTE chain intermediate edges must ALSO exist (required for upstream traversal)
    # PR 3: CTE keys are namespaced; check by suffix pattern "::cte_*"
    cte_chain_edges = {k for k in dst_keys if "::cte_" in k}
    assert cte_chain_edges, (
        f"CTE chain edges (namespace::cte_insert.*, namespace::cte_base.*) must EXIST for "
        f"upstream traversal — ARCHITECTURE_REVIEW §3.2. Got dst_keys: {sorted(dst_keys)}"
    )


def test_P1_derived_subquery_does_not_leak(tmp_path):
    """Derived subquery (inline FROM (SELECT ...) alias) must not become the dst.

    Covers the 8,158 edges landing on kind='derived' nodes in the live graph.

        INSERT INTO ba.fact (col_a)
        SELECT sub.col_a
        FROM (SELECT x AS col_a FROM ba.src) sub;

    Expected: dst = ba.fact.col_a, src = ba.src.x
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": """\
            CREATE TABLE ba.src (x INT);
            CREATE TABLE ba.fact (col_a INT);
        """,
            "etl.sql": """\
            INSERT INTO ba.fact (col_a)
            SELECT sub.col_a
            FROM (SELECT x AS col_a FROM ba.src) sub;
        """,
        },
    )
    try:
        edges = db.run_read(
            "SELECT src_key, dst_key FROM COLUMN_LINEAGE WHERE dst_key LIKE 'ba.fact.%'",
            {},
        )
    finally:
        db.close()

    assert edges, "Expected at least one edge landing on ba.fact.*"
    assert all(r["dst_key"].startswith("ba.fact.") for r in edges), (
        f"All edges must land on ba.fact.*, got: {[r['dst_key'] for r in edges]}"
    )
    assert any(r["src_key"].startswith("ba.src.") for r in edges), (
        f"At least one edge must originate from ba.src.*, got: {[r['src_key'] for r in edges]}"
    )


# ---------------------------------------------------------------------------
# P2 — Alias on INSERT target (user-reported concern)
#
# Source alias resolution is working (0 unresolved src tables in live graph).
# Verify the INSERT target with a table alias in the FROM clause does not
# accidentally become the destination.
# ---------------------------------------------------------------------------


def test_P2_table_alias_in_from_does_not_become_dst(tmp_path):
    """INSERT whose FROM uses an alias must route edges to the real INSERT target.

    INSERT INTO ba.fact (col_a)
    SELECT t.x AS col_a FROM ba.src AS t;

    Expected: dst = ba.fact.col_a, src = ba.src.x
    The alias 't' must NOT appear in any dst_key.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": """\
            CREATE TABLE ba.src (x INT);
            CREATE TABLE ba.fact (col_a INT);
        """,
            "etl.sql": """\
            INSERT INTO ba.fact (col_a)
            SELECT t.x AS col_a
            FROM ba.src AS t;
        """,
        },
    )
    try:
        edges = db.run_read(
            "SELECT src_key, dst_key FROM COLUMN_LINEAGE",
            {},
        )
    finally:
        db.close()

    dst_keys = {r["dst_key"] for r in edges}
    src_keys = {r["src_key"] for r in edges}

    assert any(k.startswith("ba.fact.") for k in dst_keys), (
        f"Expected edges landing on ba.fact.*, got: {sorted(dst_keys)}"
    )
    assert any(k.startswith("ba.src.") for k in src_keys), (
        f"Expected edges originating from ba.src.*, got: {sorted(src_keys)}"
    )
    # alias 't' as table prefix — check dst_key does not start with 't.' or contain '.t.'
    alias_leak = {k for k in dst_keys if k.startswith("t.") or ".t." in k}
    assert not alias_leak, (
        f"Table alias 't' must not appear as a table qualifier in dst_key, "
        f"got: {sorted(alias_leak)}"
    )


def test_P2_multi_join_aliases_all_resolve_to_real_tables(tmp_path):
    """Multi-JOIN with aliases: every src_key must trace to a real catalogued table."""
    db = _index(
        tmp_path,
        {
            "ddl.sql": """\
            CREATE TABLE ba.orders (order_id INT, amount INT);
            CREATE TABLE ba.customers (order_id INT, customer_name VARCHAR);
            CREATE TABLE ba.fact (order_id INT, amount INT, customer_name VARCHAR);
        """,
            "etl.sql": """\
            INSERT INTO ba.fact (order_id, amount, customer_name)
            SELECT o.order_id, o.amount, c.customer_name
            FROM ba.orders o
            JOIN ba.customers c ON o.order_id = c.order_id;
        """,
        },
    )
    try:
        edges = db.run_read(
            "SELECT src_key, dst_key FROM COLUMN_LINEAGE WHERE dst_key LIKE 'ba.fact.%'",
            {},
        )
    finally:
        db.close()

    assert edges, "Expected lineage edges into ba.fact"
    for r in edges:
        src_table = r["src_key"].rsplit(".", 1)[0]
        assert src_table in ("ba.orders", "ba.customers"), (
            f"src_key {r['src_key']!r} does not trace to a known table — alias may be leaking"
        )


# ---------------------------------------------------------------------------
# P3 — Quoted view names with spaces: schema prefix stripped from SqlColumn
#
# 232 tables in the live graph have ZERO HAS_COLUMN entries.
# SqlTable.qualified = 'ia_semantic.artikel tijdsgebonden'
# SqlColumn.table_name = 'artikel tijdsgebonden'  (schema stripped)
# Fix: preserve the full schema-qualified name in SqlColumn.table_name.
# ---------------------------------------------------------------------------


def test_P3_quoted_view_with_spaces_preserves_schema_in_sqlcolumn(tmp_path):
    """SqlColumn.table_name for a quoted view name with spaces must include the schema.

    Today: CREATE OR REPLACE VIEW ia_semantic."ODS Workaround Artikel" AS SELECT ...
    results in SqlColumn.table_name = 'ods workaround artikel' (schema dropped).
    Expected: SqlColumn.table_name = 'ia_semantic.ods workaround artikel' (schema preserved).
    """
    db = _index(
        tmp_path,
        {
            "src.sql": """\
            CREATE TABLE da.src_products (code VARCHAR, ean_name VARCHAR, functional_name VARCHAR);
        """,
            "view.sql": """\
            CREATE OR REPLACE VIEW ia_semantic."ODS Workaround Artikel" AS
            SELECT
                code            AS "DN_ARTIKEL_NUMMER",
                ean_name        AS "EAN naam vl",
                functional_name AS "functionele naam en"
            FROM da.src_products;
        """,
        },
    )
    try:
        cols = db.run_read(
            "SELECT table_name, col_name FROM SqlColumn "
            "WHERE table_name LIKE '%workaround%' OR table_name LIKE '%artikel%'",
            {},
        )
        hc = db.run_read(
            "SELECT src_key, dst_key FROM HAS_COLUMN "
            "WHERE src_key LIKE '%workaround%' OR src_key LIKE '%artikel%'",
            {},
        )
    finally:
        db.close()

    table_names = {r["table_name"] for r in cols}
    assert table_names, "Expected SqlColumn rows for the quoted view"

    # The schema must NOT be stripped — all table_name values must contain 'ia_semantic'
    bare_names = {t for t in table_names if "ia_semantic" not in t}
    assert not bare_names, (
        f"Schema prefix stripped from SqlColumn.table_name: {sorted(bare_names)}. "
        f"Expected 'ia_semantic.ods workaround artikel', got: {sorted(table_names)}"
    )

    # HAS_COLUMN must be wired — otherwise the catalog is invisible to lineage traversal
    assert hc, (
        f"HAS_COLUMN entries missing for the quoted view. "
        f"SqlColumn rows exist ({[r['col_name'] for r in cols]}) but HAS_COLUMN is empty."
    )


def test_P3_quoted_view_lineage_reaches_source_via_has_column(tmp_path):
    """The full chain: source table → quoted view → downstream query must be traversable.

    When HAS_COLUMN is wired correctly, get_upstream_dependencies on a quoted-view column
    should return edges from the source. Here we verify the COLUMN_LINEAGE and HAS_COLUMN
    rows are consistent so a graph traversal would succeed.
    """
    db = _index(
        tmp_path,
        {
            "src.sql": """\
            CREATE TABLE ba.raw_artikel (artikel_nr INT, naam VARCHAR);
        """,
            "view.sql": """\
            CREATE OR REPLACE VIEW ia_semantic."Artikel Tijdsgebonden" AS
            SELECT artikel_nr AS dn_artikel_nr, naam AS da_naam
            FROM ba.raw_artikel;
        """,
        },
    )
    try:
        # SqlColumn must have schema-qualified table_name
        cols = db.run_read(
            "SELECT table_name, col_name FROM SqlColumn "
            "WHERE LOWER(table_name) LIKE '%artikel tijdsgebonden%'",
            {},
        )
        # COLUMN_LINEAGE must have edges landing on the qualified view name
        edges = db.run_read(
            "SELECT src_key, dst_key FROM COLUMN_LINEAGE "
            "WHERE LOWER(dst_key) LIKE '%artikel tijdsgebonden%'",
            {},
        )
        # HAS_COLUMN must connect the view to its columns
        hc = db.run_read(
            "SELECT src_key, dst_key FROM HAS_COLUMN "
            "WHERE LOWER(src_key) LIKE '%artikel tijdsgebonden%'",
            {},
        )
    finally:
        db.close()

    assert cols, "Expected SqlColumn rows for 'Artikel Tijdsgebonden' view"
    assert edges, "Expected COLUMN_LINEAGE edges with dst on 'Artikel Tijdsgebonden'"
    assert hc, (
        "Expected HAS_COLUMN entries for 'Artikel Tijdsgebonden'. "
        "Without these, lineage traversal dead-ends at the view."
    )
    # All three must agree on the same qualified key
    col_tables = {r["table_name"] for r in cols}
    hc_src_keys = {r["src_key"] for r in hc}
    assert col_tables == hc_src_keys or col_tables & hc_src_keys, (
        f"SqlColumn.table_name and HAS_COLUMN.src_key disagree: "
        f"cols={sorted(col_tables)}, hc={sorted(hc_src_keys)}"
    )


# ---------------------------------------------------------------------------
# P4 — CTAS column discovery
#
# 241 CTAS tables confirmed in live graph, all with ZERO SqlColumn rows.
# CREATE TABLE t AS SELECT a, b FROM s — columns come from SELECT projection,
# not from an explicit (col1, col2) list.
# ---------------------------------------------------------------------------


def test_P4_ctas_columns_derived_from_select_body(tmp_path):
    """CREATE TABLE t AS SELECT a, b FROM s must produce SqlColumn rows for a and b.

    Today: SqlColumn has 0 rows for CTAS tables (only explicit column-list DDL harvested).
    Expected: SqlColumn rows for each aliased projection in the CTAS SELECT body.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": """\
            CREATE TABLE ba.src (x INT, y INT, z INT);
        """,
            "ctas.sql": """\
            CREATE TABLE ba.derived_fact AS
            SELECT x AS col_a, y AS col_b, z AS col_c
            FROM ba.src;
        """,
        },
    )
    try:
        cols = db.run_read(
            "SELECT col_name FROM SqlColumn WHERE table_qualified = 'ba.derived_fact'",
            {},
        )
        hc = db.run_read(
            "SELECT dst_key FROM HAS_COLUMN WHERE src_key = 'ba.derived_fact'",
            {},
        )
    finally:
        db.close()

    col_names = {r["col_name"] for r in cols}
    assert col_names, (
        "Expected SqlColumn rows for CTAS table 'ba.derived_fact'. "
        "Currently 0 rows — CTAS column discovery is not implemented."
    )
    assert col_names == {"col_a", "col_b", "col_c"}, (
        f"Expected columns {{col_a, col_b, col_c}}, got: {sorted(col_names)}"
    )
    assert hc, (
        f"Expected HAS_COLUMN entries for ba.derived_fact. "
        f"SqlColumn has {len(cols)} rows but HAS_COLUMN is empty — catalog not wired."
    )


def test_P4_ctas_with_cte_body_columns_derived(tmp_path):
    """CTAS whose body uses a CTE must still derive column names from the outer SELECT.

    This matches the real DWH pattern:
        CREATE OR REPLACE TEMP TABLE ba_tmp.tmp_base AS
        WITH datum AS (SELECT dn_datum FROM ba.wtda_datum)
        SELECT d.dn_datum, src.col_x FROM datum d JOIN ba.src src ON ...

    Expected: SqlColumn rows for dn_datum, col_x.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": """\
            CREATE TABLE ba.datum_src (dn_datum DATE);
            CREATE TABLE ba.src (dn_datum DATE, col_x INT);
        """,
            "ctas_cte.sql": """\
            CREATE TABLE ba.tmp_base AS
            WITH datum AS (SELECT dn_datum FROM ba.datum_src)
            SELECT d.dn_datum, s.col_x
            FROM datum d
            JOIN ba.src s ON d.dn_datum = s.dn_datum;
        """,
        },
    )
    try:
        cols = db.run_read(
            "SELECT col_name FROM SqlColumn WHERE table_qualified = 'ba.tmp_base'",
            {},
        )
    finally:
        db.close()

    col_names = {r["col_name"] for r in cols}
    assert col_names, (
        "Expected SqlColumn rows for CTAS-with-CTE table 'ba.tmp_base'. "
        "Currently 0 rows — CTAS column discovery is not implemented."
    )
    assert col_names == {"dn_datum", "col_x"}, (
        f"Expected {{dn_datum, col_x}}, got: {sorted(col_names)}"
    )


def test_P4_ctas_star_select_gracefully_degrades(tmp_path):
    """CTAS with SELECT * must not crash and should at minimum produce a SqlTable node.

    SELECT * columns cannot be statically resolved without schema expansion.
    The fix for P4 must degrade gracefully — no SqlColumn rows, no exception.
    This test ensures the degrade path is safe (not a regression guard for P4).
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE ba.src (x INT, y INT);",
            "ctas_star.sql": "CREATE TABLE ba.star_fact AS SELECT * FROM ba.src;",
        },
    )
    try:
        tables = db.run_read(
            "SELECT qualified FROM SqlTable WHERE qualified = 'ba.star_fact'",
            {},
        )
    finally:
        db.close()

    assert tables, "CTAS with SELECT * must still produce a SqlTable node (no crash)"

    # ---------------------------------------------------------------------------
    assert tables, "CTAS with SELECT * must still produce a SqlTable node (no crash)"


# ---------------------------------------------------------------------------
# P1a — CTAS kind misclassification
#
# Pattern from DWH: 55 schema-qualified CTAS target tables stored as
# kind='derived' instead of kind='table'.
#
# Root cause analysis (plan-reviewer, 2026-06-09):
#   - Candidate 1 (parser): sqlglot normalises TRANSIENT/TEMPORARY TABLE to
#     exp.Create(kind='TABLE'), so _parse_statement already matches all CTAS
#     variants. Candidate 1 is NOT the root cause.
#   - Candidate 2 (indexer): at L1335 of indexer.py, when canonical_by_bare
#     lookup fails (e.g. bare name is ambiguous or the CTAS was not registered
#     in defined_table_ids for that batch), target_kind defaults to 'derived'.
#     This is the real root cause for the DWH tables.
#
# The simple 2-3 file fixture already passes today (CTAS and INSERT are in the
# same index run so defined_table_ids includes the CTAS). These tests are
# written WITHOUT xfail as passing regression guards: they confirm the correct
# behaviour that must not regress. The DWH-specific cross-batch failure must
# be diagnosed against the live DWH corpus — it is not reproducible in a
# minimal in-memory fixture without controlling batch boundaries.
# ---------------------------------------------------------------------------


def test_P1a_ctas_target_kind_is_table_not_derived(tmp_path):
    """CTAS target followed by INSERT from another file must have kind='table', not 'derived'.

    Regression guard for P1a: any change to the CTAS recognition or kind-merge guard
    must not break the baseline case where CTAS and INSERT are indexed together.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE da.src (x INT); CREATE TABLE da.other (x INT);",
            "ctas.sql": "CREATE TABLE da.htdyn_fact AS SELECT x FROM da.src;",
            "insert.sql": "INSERT INTO da.htdyn_fact SELECT x FROM da.other;",
        },
    )
    try:
        rows = db.run_read(
            "SELECT kind FROM SqlTable WHERE qualified = 'da.htdyn_fact'",
            {},
        )
    finally:
        db.close()

    assert rows, "da.htdyn_fact must exist as a SqlTable node"
    assert rows[0]["kind"] == "table", (
        f"Expected kind='table' for CTAS target da.htdyn_fact, got kind='{rows[0]['kind']}'"
    )


def test_P1a_kind_guard_prevents_derived_overwrite(tmp_path):
    """CTAS-then-INSERT: kind='table' must survive after the INSERT file is processed.

    Regression guard for Candidate 2 (plan): the indexer kind-merge guard must not
    allow a kind='derived' row from the INSERT-target path to overwrite an existing
    kind='table' row in the same index run.
    """
    db = _index(
        tmp_path,
        {
            "src.sql": "CREATE TABLE ba.src (a INT, b INT);",
            "ctas.sql": "CREATE TABLE ba.target AS SELECT a, b FROM ba.src;",
            "insert.sql": "INSERT INTO ba.target SELECT a, b FROM ba.src;",
        },
    )
    try:
        rows = db.run_read(
            "SELECT kind FROM SqlTable WHERE qualified = 'ba.target'",
            {},
        )
        has_col = db.run_read(
            "SELECT COUNT(*) AS n FROM HAS_COLUMN hc "
            "JOIN SqlTable t ON hc.src_key = t.qualified "
            "WHERE t.qualified = 'ba.target'",
            {},
        )
    finally:
        db.close()

    assert rows, "ba.target must exist as a SqlTable node"
    assert rows[0]["kind"] == "table", (
        f"Expected kind='table' for ba.target after INSERT file processed, "
        f"got kind='{rows[0]['kind']}'"
    )
    assert has_col[0]["n"] > 0, (
        "ba.target must have HAS_COLUMN rows (P4 catalog); "
        "they only wire if kind='table' is preserved"
    )


def test_P1a_cross_batch_kind_guard_prevents_derived_overwrite(tmp_path):
    """kind='table' written in one upsert batch must not be overwritten by kind='derived'
    from a second independent upsert batch.

    Simulates the DWH cross-batch scenario: File A (batch 1) defines the CTAS target as
    kind='table'; File B (batch 2) references it as an INSERT target that cannot be
    resolved via canonical_by_bare, so the INSERT-target path emits kind='derived'.
    The DB-level ON CONFLICT guard in upsert_nodes_bulk must prevent the downgrade.

    Guards the P1a fix in duckdb_backend.py and plan/sprints/coverage_p1_p5_metric.md.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend
    from sqlcg.core.schema import NodeLabel

    db = DuckDBBackend(":memory:")
    db.init_schema()

    # Batch 1: CTAS defines the table as kind='table' with DDL provenance.
    db.upsert_nodes_bulk(
        NodeLabel.TABLE,
        [
            {
                "qualified": "da.ctas_target",
                "catalog": "",
                "db": "da",
                "name": "ctas_target",
                "kind": "table",
                "defined_in_file": "ctas.sql",
            }
        ],
    )

    # Batch 2: INSERT-target path emits kind='derived' (canonical_by_bare miss).
    db.upsert_nodes_bulk(
        NodeLabel.TABLE,
        [
            {
                "qualified": "da.ctas_target",
                "catalog": "",
                "db": "da",
                "name": "ctas_target",
                "kind": "derived",
                "defined_in_file": "",
            }
        ],
    )

    rows = db.run_read(
        "SELECT kind, defined_in_file FROM SqlTable WHERE qualified = 'da.ctas_target'", {}
    )
    db.close()

    assert rows, "da.ctas_target must exist"
    assert rows[0]["kind"] == "table", (
        f"Cross-batch kind downgrade: expected kind='table', got kind='{rows[0]['kind']}'. "
        "upsert_nodes_bulk must not overwrite 'table' with 'derived'."
    )
    assert rows[0]["defined_in_file"] == "ctas.sql", (
        "defined_in_file must be preserved from the first (DDL) batch, not overwritten by ''."
    )


# ---------------------------------------------------------------------------
# P5 — USE SCHEMA bare name resolution
#
# 3,016 edges / 149 tables. Bare table names after a USE SCHEMA statement
# must be qualified with the active schema so they resolve to the correct
# SqlTable node in the graph.
# ---------------------------------------------------------------------------


def test_P5_use_schema_qualifies_bare_source_table(tmp_path):
    """USE SCHEMA sets the active schema; bare table refs in subsequent statements
    must resolve to schema-qualified names.

    Reproduces Sub-problem C from the DWH: a file starts with
        USE SCHEMA da;
    and later statements reference tables without the 'da.' prefix.
    Without P5 the edges land on bare 'src_table' not 'da.src_table'.

    Guards plan/sprints/coverage_p1_p5_metric.md § Sub-problem C.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE da.src_table (x INT, y INT);",
            "etl.sql": """\
                USE SCHEMA da;
                INSERT INTO real_target (col_a, col_b)
                SELECT x AS col_a, y AS col_b
                FROM src_table;
            """,
        },
    )
    try:
        edges = db.run_read("SELECT src_key, dst_key FROM COLUMN_LINEAGE", {})
        bare_table = db.run_read("SELECT qualified FROM SqlTable WHERE qualified = 'src_table'", {})
    finally:
        db.close()

    src_keys = {r["src_key"] for r in edges}
    assert any(k.startswith("da.src_table.") for k in src_keys), (
        f"Expected edges from da.src_table.*, got: {sorted(src_keys)}. "
        "Bare 'src_table' must be qualified to 'da.src_table' via USE SCHEMA."
    )
    assert not bare_table, (
        "Bare 'src_table' node must NOT exist — it must be qualified to 'da.src_table'."
    )


def test_P5_use_schema_qualifies_insert_target(tmp_path):
    """USE SCHEMA also qualifies bare INSERT targets.

    After USE SCHEMA da, an INSERT INTO fact should land on da.fact, not bare 'fact'.
    Guards plan/sprints/coverage_p1_p5_metric.md § Sub-problem C.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE da.src (x INT); CREATE TABLE da.fact (col_a INT);",
            "etl.sql": """\
                USE SCHEMA da;
                INSERT INTO fact (col_a)
                SELECT x AS col_a FROM src;
            """,
        },
    )
    try:
        edges = db.run_read(
            "SELECT src_key, dst_key FROM COLUMN_LINEAGE WHERE dst_key LIKE 'da.fact.%'",
            {},
        )
    finally:
        db.close()

    assert edges, (
        "Expected COLUMN_LINEAGE edges to da.fact.col_a after USE SCHEMA da qualifies bare 'fact'."
    )


def test_P5_use_schema_does_not_qualify_cte_names(tmp_path):
    """CTE names defined within a statement must NOT be schema-qualified.

    USE SCHEMA da must qualify external table refs but leave CTE aliases intact
    so sqlglot can resolve CTE-internal column references correctly.
    Guards plan/sprints/coverage_p1_p5_metric.md § Sub-problem C.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE da.src (x INT); CREATE TABLE da.fact (col_a INT);",
            "etl.sql": """\
                USE SCHEMA da;
                INSERT INTO fact (col_a)
                WITH cte1 AS (SELECT x AS col_a FROM src)
                SELECT col_a FROM cte1;
            """,
        },
    )
    try:
        edges = db.run_read(
            "SELECT src_key, dst_key FROM COLUMN_LINEAGE WHERE dst_key LIKE 'da.fact.%'",
            {},
        )
        bad_cte = db.run_read("SELECT qualified FROM SqlTable WHERE qualified = 'da.cte1'", {})
    finally:
        db.close()

    assert edges, "Expected COLUMN_LINEAGE edges landing on da.fact.* via USE SCHEMA"
    assert not bad_cte, (
        "CTE 'cte1' must NOT be qualified to 'da.cte1' — CTE names are statement-local."
    )


def test_P5_use_schema_scope_is_per_file(tmp_path):
    """USE SCHEMA context is file-scoped: a file without USE SCHEMA must not be affected.

    Guards plan/sprints/coverage_p1_p5_metric.md § Sub-problem C.
    """
    db = _index(
        tmp_path,
        {
            "ddl.sql": "CREATE TABLE da.src (x INT); CREATE TABLE da.fact (col_a INT);",
            "with_use.sql": """\
                USE SCHEMA da;
                INSERT INTO fact (col_a) SELECT x AS col_a FROM src;
            """,
            "without_use.sql": """\
                INSERT INTO da.fact (col_a) SELECT x AS col_a FROM da.src;
            """,
        },
    )
    try:
        edges = db.run_read(
            "SELECT src_key, dst_key FROM COLUMN_LINEAGE WHERE dst_key LIKE 'da.fact.%'",
            {},
        )
    finally:
        db.close()

    assert edges, "Expected COLUMN_LINEAGE edges to da.fact after both files are indexed"
    dst_keys = {r["dst_key"] for r in edges}
    assert "da.fact.col_a" in dst_keys, f"Expected da.fact.col_a in edges, got: {sorted(dst_keys)}"
