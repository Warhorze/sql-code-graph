"""Integration tests for PR 4 Step 4.2 — catalog-aware kind upgrade.

After an INFORMATION_SCHEMA catalog is loaded, SqlTable rows that are
mis-kinded as 'derived' (DDL lives in un-indexed files) must be upgraded to
'table'.  The upgrade must fire in BOTH catalog-application seams:
  1. apply_catalog_to_backend() (the standalone `sqlcg catalog load` path)
  2. _reapply_catalog_if_configured() (the post-index hook)

Amendment A4 acceptance criteria:
- kind 'derived' → 'table' when the table appears in the catalog.
- kind 'table' (DDL-sourced) is NOT downgraded.
- kind 'view' is NOT touched.
- After upgrade, _Q_CTE_COLLISIONS returns 0 for the upgraded tables.

([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sqlcg.cli.commands.catalog import apply_catalog_to_backend
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.schema import NodeLabel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend():
    b = DuckDBBackend(":memory:")
    b.init_schema()
    yield b
    b.close()


def _catalog_csv(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    p = tmp_path / "cols.csv"
    lines = ["TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME"]
    for schema, table, col in rows:
        lines.append(f"{schema},{table},{col}")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _seed_table(backend: DuckDBBackend, qualified: str, kind: str) -> None:
    backend.upsert_nodes_bulk(
        NodeLabel.TABLE,
        [
            {
                "qualified": qualified,
                "catalog": "",
                "db": qualified.split(".")[0] if "." in qualified else "",
                "name": qualified.split(".")[-1],
                "kind": kind,
                "defined_in_file": "",
            }
        ],
    )


def _get_kind(backend: DuckDBBackend, qualified: str) -> str | None:
    rows = backend.run_read(
        'SELECT kind FROM "SqlTable" WHERE qualified = ?',
        {"q": qualified},
    )
    return rows[0]["kind"] if rows else None


# ---------------------------------------------------------------------------
# upgrade_derived_to_table_for_keys — backend method
# ---------------------------------------------------------------------------


class TestUpgradeDerivedToTableMethod:
    """DuckDBBackend.upgrade_derived_to_table_for_keys behaviour.

    ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2)
    """

    def test_derived_upgraded_to_table(self, backend: DuckDBBackend):
        """A derived-kinded table is upgraded to 'table' when its key is in the list."""
        _seed_table(backend, "ba.some_view_target", "derived")

        backend.upgrade_derived_to_table_for_keys(["ba.some_view_target"])

        assert _get_kind(backend, "ba.some_view_target") == "table", (
            "Expected kind to be upgraded from 'derived' to 'table'."
        )

    def test_ddl_table_not_downgraded(self, backend: DuckDBBackend):
        """A table already kinded 'table' (from DDL) is NOT modified by the upgrade.

        A4 guard: WHERE kind='derived' must be the sole filter.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2 — A4)
        """
        _seed_table(backend, "ba.ddl_table", "table")

        backend.upgrade_derived_to_table_for_keys(["ba.ddl_table"])

        # Still 'table' — not an accidental re-write.
        assert _get_kind(backend, "ba.ddl_table") == "table"

    def test_view_not_touched(self, backend: DuckDBBackend):
        """A view-kinded table is NOT modified by the upgrade.

        A4 guard: only 'derived' → 'table' allowed, never view/table.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2 — A4)
        """
        _seed_table(backend, "ba.my_view", "view")

        backend.upgrade_derived_to_table_for_keys(["ba.my_view"])

        assert _get_kind(backend, "ba.my_view") == "view", (
            "view-kinded tables must NEVER be touched by the kind upgrade."
        )

    def test_empty_key_list_is_noop(self, backend: DuckDBBackend):
        """Calling with an empty list does not raise and returns 0."""
        result = backend.upgrade_derived_to_table_for_keys([])
        assert result == 0


# ---------------------------------------------------------------------------
# Seam 1: apply_catalog_to_backend (catalog load command path)
# ---------------------------------------------------------------------------


class TestCatalogLoadSeamUpgrade:
    """The catalog load command seam fires the kind upgrade (A4 seam 1).

    ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2 — A4)
    """

    def test_derived_upgraded_via_catalog_load(self, backend: DuckDBBackend, tmp_path: Path):
        """A derived-kinded table is upgraded after apply_catalog_to_backend.

        Simulates a table whose DDL is in un-indexed Liquibase XML — it was
        written as 'derived' during indexing because the parser could not resolve it.
        After catalog load the kind must flip to 'table'.
        """
        _seed_table(backend, "ba.wtfv_cyclische_telling", "derived")
        csv_path = _catalog_csv(tmp_path, [("ba", "wtfv_cyclische_telling", "id_col")])

        result = apply_catalog_to_backend(csv_path, backend)

        # kind upgraded.
        assert _get_kind(backend, "ba.wtfv_cyclische_telling") == "table", (
            "Expected 'derived' → 'table' after catalog load."
        )
        # Return dict includes the derived_upgraded count.
        assert result["derived_upgraded"] >= 1

    def test_ddl_table_not_downgraded_via_catalog_load(
        self, backend: DuckDBBackend, tmp_path: Path
    ):
        """A DDL-kinded table survives catalog load unchanged (A4 guard).

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2 — A4)
        """
        _seed_table(backend, "ba.real_ddl_table", "table")
        csv_path = _catalog_csv(tmp_path, [("ba", "real_ddl_table", "col_x")])

        apply_catalog_to_backend(csv_path, backend)

        # Still 'table' — A4 guard preserved it.
        assert _get_kind(backend, "ba.real_ddl_table") == "table"

    def test_view_not_touched_via_catalog_load(self, backend: DuckDBBackend, tmp_path: Path):
        """A view-kinded table is not modified by catalog load (A4 guard).

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2 — A4)
        """
        _seed_table(backend, "ba.my_view", "view")
        csv_path = _catalog_csv(tmp_path, [("ba", "my_view", "view_col")])

        apply_catalog_to_backend(csv_path, backend)

        assert _get_kind(backend, "ba.my_view") == "view", (
            "view-kinded table must survive catalog load unchanged."
        )

    def test_cte_collision_query_returns_zero_after_upgrade(
        self, backend: DuckDBBackend, tmp_path: Path
    ):
        """After upgrading a derived table, _Q_CTE_COLLISIONS returns 0 for it.

        Before upgrade: the 'derived'-kinded table appears in the collision query
        denominator; after upgrade its kind is 'table' so the CTE collision query
        no longer counts it.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2)
        """
        from sqlcg.cli.coverage import _Q_CTE_COLLISIONS

        # Seed the derived table.
        _seed_table(backend, "ba.phantom_target", "derived")

        # Seed two SqlQuery rows pretending to write to the derived table.
        backend._conn.execute(
            """
            INSERT INTO "File" (path) VALUES ('f1.sql'), ('f2.sql')
            """
        )
        backend._conn.execute(
            """
            INSERT INTO "Repo" (path) VALUES ('/repo')
            """
        )
        backend._conn.execute(
            """
            INSERT INTO "SqlColumn" (id, col_name, table_qualified, catalog, db, table_name)
            VALUES
              ('ba.phantom_target.col_a', 'col_a', 'ba.phantom_target', '', 'ba', 'phantom_target')
            """
        )
        backend._conn.execute(
            """
            INSERT INTO "SqlQuery" (id, file_path, kind, parse_failed)
            VALUES
              ('q1', 'f1.sql', 'INSERT', false),
              ('q2', 'f2.sql', 'INSERT', false)
            """
        )
        # Two different src_keys write to the same dst_key from two different files.
        # This simulates the CTE-collision scenario: same dst_key, two file_paths.
        backend._conn.execute(
            # noqa: E501 — SQL column list intentionally verbose for clarity
            'INSERT INTO "COLUMN_LINEAGE" '
            "(query_id, src_key, dst_key, confidence, inferred_from_source_name) VALUES "
            "('q1', 'src1.tbl.col_a', 'ba.phantom_target.col_a', 0.9, false), "
            "('q2', 'src2.tbl.col_a', 'ba.phantom_target.col_a', 0.9, false)"
        )

        # Before upgrade: collision query counts the derived table.
        before = backend.run_read(_Q_CTE_COLLISIONS, {})
        before_count = before[0]["cte_collisions"] if before else 0
        assert before_count >= 1, (
            f"Expected at least 1 collision before upgrade (derived table present), "
            f"got {before_count}."
        )

        # Load catalog → triggers kind upgrade.
        csv_path = _catalog_csv(tmp_path, [("ba", "phantom_target", "col_a")])
        apply_catalog_to_backend(csv_path, backend)

        # After upgrade: the table is now 'table'-kinded, not in the collision query.
        after = backend.run_read(_Q_CTE_COLLISIONS, {})
        after_count = after[0]["cte_collisions"] if after else 0
        assert after_count == 0, f"Expected 0 CTE collisions after kind upgrade, got {after_count}."


# ---------------------------------------------------------------------------
# Seam 2: _reapply_catalog_if_configured (post-index hook path)
# ---------------------------------------------------------------------------


class TestReapplyCatalogSeamUpgrade:
    """The _reapply_catalog_if_configured post-index hook fires the kind upgrade (A4 seam 2).

    ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2 — A4)
    """

    def test_derived_upgraded_via_reapply_hook(self, backend: DuckDBBackend, tmp_path: Path):
        """A derived-kinded table is upgraded after _reapply_catalog_if_configured.

        Simulates the post-index hook path: a .sqlcg.toml configures a catalog
        path; after indexing the hook fires apply_catalog_to_backend, which must
        also upgrade derived→table.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2 — A4, seam 2)
        """
        from sqlcg.indexer.indexer import Indexer

        # Seed the table as 'derived' (as would be left by the parser when
        # the DDL is in un-indexed XML).
        _seed_table(backend, "ba.wtfi_promotie_afzet", "derived")

        # Write the catalog CSV.
        csv_path = _catalog_csv(tmp_path, [("ba", "wtfi_promotie_afzet", "revenue")])

        indexer = Indexer()

        # Patch get_catalog_path (module-level import in indexer.py) and get_schema_aliases
        # (imported inside the function from sqlcg.core.config).
        with (
            patch("sqlcg.indexer.indexer.get_catalog_path", return_value=csv_path),
            patch("sqlcg.core.config.get_schema_aliases", return_value={}),
        ):
            indexer._reapply_catalog_if_configured(backend, tmp_path)

        # kind must be upgraded.
        assert _get_kind(backend, "ba.wtfi_promotie_afzet") == "table", (
            "Expected 'derived' → 'table' after _reapply_catalog_if_configured."
        )

    def test_ddl_table_not_downgraded_via_reapply_hook(
        self, backend: DuckDBBackend, tmp_path: Path
    ):
        """A DDL-kinded table survives _reapply_catalog_if_configured unchanged.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.2 — A4, seam 2)
        """
        from sqlcg.indexer.indexer import Indexer

        _seed_table(backend, "ba.ddl_real_table", "table")
        csv_path = _catalog_csv(tmp_path, [("ba", "ddl_real_table", "col_x")])
        indexer = Indexer()

        with (
            patch("sqlcg.indexer.indexer.get_catalog_path", return_value=csv_path),
            patch("sqlcg.core.config.get_schema_aliases", return_value={}),
        ):
            indexer._reapply_catalog_if_configured(backend, tmp_path)

        assert _get_kind(backend, "ba.ddl_real_table") == "table"
