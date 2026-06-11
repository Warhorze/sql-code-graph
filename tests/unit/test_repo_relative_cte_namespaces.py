"""Unit tests for repo-relative CTE/derived namespace keys (PR 3).

After sprint_postmortem_fixes §PR 3 Step 3.1, CTE/derived `TableRef.full_id` keys
produced by both AnsiParser and SnowflakeParser must use the repo-relative posix path
supplied via `rel_path`, not the absolute OS path.

Guards plan/sprints/sprint_postmortem_fixes.md §PR 3.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Unit: rel_path → relative key, not absolute
# ---------------------------------------------------------------------------


class TestRelPathProducesRelativeNamespace:
    """parse_file(rel_path=...) stamps CTE keys as <rel_path>::<name>.

    Guards sprint_postmortem_fixes.md §PR 3 Step 3.1.
    """

    def test_ansi_cte_key_is_repo_relative(self, tmp_path):
        """ANSI parser produces 'subdir/fixture.sql::my_cte' when rel_path is supplied.

        The absolute path prefix must not appear in the CTE node qualified key.
        Guards sprint_postmortem_fixes.md §PR 3 Step 3.1.
        """
        from sqlcg.lineage.schema_resolver import SchemaResolver
        from sqlcg.parsers.ansi_parser import AnsiParser

        sql = (
            "CREATE TABLE src (a INT);\n"
            "INSERT INTO dst\n"
            "WITH my_cte AS (SELECT a FROM src)\n"
            "SELECT a FROM my_cte;\n"
        )
        abs_path = tmp_path / "subdir" / "fixture.sql"
        expected_rel = "subdir/fixture.sql"

        parser = AnsiParser(SchemaResolver(dialect=None))
        result = parser.parse_file(abs_path, sql, rel_path=expected_rel)

        # Gather all CTE/derived TableRef.full_id values from column lineage
        cte_keys: list[str] = []
        for stmt in result.statements:
            for edge in stmt.column_lineage:
                for ref in (edge.src, edge.dst):
                    if ref.table.role in ("cte", "derived") and "::" in ref.table.full_id:
                        cte_keys.append(ref.table.full_id)

        assert cte_keys, (
            "No CTE/derived keys found in column lineage — fixture must produce at least one."
        )
        for key in cte_keys:
            assert key.startswith(expected_rel + "::"), (
                f"CTE key {key!r} does not start with repo-relative prefix "
                f"'{expected_rel}::'. Absolute path must not appear in CTE keys."
            )
            assert not key.startswith("/"), (
                f"CTE key {key!r} starts with '/' — absolute path leaked into namespace."
            )

    def test_ansi_cte_key_no_rel_path_falls_back_to_str_path(self, tmp_path):
        """Without rel_path, the fallback is str(path) (legacy behaviour preserved).

        This is the reindex_file / single-file path. The fallback is a known
        limitation documented in the PR body; the full-index path always supplies rel_path.
        Guards sprint_postmortem_fixes.md §PR 3 Step 3.1 (fallback branch).
        """
        from sqlcg.lineage.schema_resolver import SchemaResolver
        from sqlcg.parsers.ansi_parser import AnsiParser

        sql = "INSERT INTO dst\nWITH my_cte AS (SELECT a FROM src)\nSELECT a FROM my_cte;\n"
        abs_path = tmp_path / "fixture.sql"

        parser = AnsiParser(SchemaResolver(dialect=None))
        result = parser.parse_file(abs_path, sql)  # no rel_path supplied

        cte_keys: list[str] = []
        for stmt in result.statements:
            for edge in stmt.column_lineage:
                for ref in (edge.src, edge.dst):
                    if ref.table.role in ("cte", "derived") and "::" in ref.table.full_id:
                        cte_keys.append(ref.table.full_id)

        # When no rel_path, should fall back to str(path) — verify it contains the filename
        for key in cte_keys:
            assert "fixture.sql" in key, (
                f"Fallback CTE key {key!r} does not contain the filename 'fixture.sql'."
            )


class TestPortabilityAcrossAbsoluteRoots:
    """Same relative layout under two different absolute roots yields identical CTE keys.

    Guards sprint_postmortem_fixes.md §PR 3 Step 3.1 (portability requirement).
    """

    def test_identical_keys_under_two_different_abs_roots(self, tmp_path):
        """Parsing 'etl/file.sql' from two different absolute repos gives the same key.

        The CTE/derived keys must be purely relative — machine-dependent prefixes
        must not affect graph identity.
        Guards sprint_postmortem_fixes.md §PR 3.
        """
        from sqlcg.lineage.schema_resolver import SchemaResolver
        from sqlcg.parsers.ansi_parser import AnsiParser

        sql = "INSERT INTO dst\nWITH cte_x AS (SELECT col FROM src)\nSELECT col FROM cte_x;\n"
        rel = "etl/file.sql"

        # Root A
        root_a = tmp_path / "project_a"
        root_a.mkdir()
        abs_a = root_a / "etl" / "file.sql"

        # Root B — entirely different absolute prefix
        root_b = tmp_path / "project_b"
        root_b.mkdir()
        abs_b = root_b / "etl" / "file.sql"

        parser = AnsiParser(SchemaResolver(dialect=None))

        result_a = parser.parse_file(abs_a, sql, rel_path=rel)
        result_b = parser.parse_file(abs_b, sql, rel_path=rel)

        def _cte_keys(result) -> set[str]:
            keys: set[str] = set()
            for stmt in result.statements:
                for edge in stmt.column_lineage:
                    for ref in (edge.src, edge.dst):
                        if ref.table.role in ("cte", "derived") and "::" in ref.table.full_id:
                            keys.add(ref.table.full_id)
            return keys

        keys_a = _cte_keys(result_a)
        keys_b = _cte_keys(result_b)

        assert keys_a, "No CTE keys produced from root A — fixture must produce at least one."
        assert keys_a == keys_b, (
            f"CTE keys differ across two absolute roots with the same relative layout.\n"
            f"Root A keys: {keys_a}\n"
            f"Root B keys: {keys_b}\n"
            f"Keys must be identical when rel_path is the same."
        )
        for key in keys_a:
            assert key.startswith("etl/file.sql::"), (
                f"Key {key!r} does not start with expected relative prefix 'etl/file.sql::'."
            )


# ---------------------------------------------------------------------------
# (A3) Consumer round-trip: _harvest_usage_catalog and noise_filter
# ---------------------------------------------------------------------------


class TestConsumerRoundTrip:
    """After re-keying to relative namespaces, consumers behave correctly.

    Guards sprint_postmortem_fixes.md §PR 3 Step 3.0 (A3 consumer audit).
    """

    def test_harvest_usage_catalog_zero_relative_cte_keys_harvested(self):
        """_harvest_usage_catalog harvests ZERO ::- keys into HAS_COLUMN.

        Relative namespaced keys ('subdir/f.sql::my_cte') must not be harvested
        into the usage catalog — the '::'- guard is format-agnostic and still fires.
        Guards sprint_postmortem_fixes.md §PR 3 Step 3.0 (A3).
        """
        from sqlcg.indexer.indexer import _harvest_usage_catalog

        # Simulate COLUMN_LINEAGE edges with a relative-namespaced CTE src key
        edges = [
            {
                "src_key": "subdir/fact.sql::my_cte.revenue",
                "dst_key": "ba.target_table.revenue",
            }
        ]
        kind_map = {
            "subdir/fact.sql::my_cte": "cte",  # correct kind
        }
        col_rows, hc_edges = _harvest_usage_catalog(edges, kind_map)
        assert col_rows == [], (
            f"Relative-namespaced CTE src key must NOT be harvested. Got: {col_rows}"
        )
        assert hc_edges == [], (
            f"Relative-namespaced CTE src key must NOT produce HAS_COLUMN edges. Got: {hc_edges}"
        )

    def test_harvest_usage_catalog_physical_table_still_harvested(self):
        """Physical table src keys are still harvested after the rel-path change.

        The '::'- guard only blocks namespaced keys; physical tables pass through.
        Guards sprint_postmortem_fixes.md §PR 3 Step 3.0 (A3).
        """
        from sqlcg.indexer.indexer import _harvest_usage_catalog

        edges = [
            {
                "src_key": "ba.staging_table.col_a",
                "dst_key": "ba.target_table.col_a",
            }
        ]
        kind_map = {"ba.staging_table": "table"}
        col_rows, hc_edges = _harvest_usage_catalog(edges, kind_map)
        assert len(col_rows) == 1, (
            f"Physical table src key must still be harvested. Got: {col_rows}"
        )

    def test_noise_filter_filters_relative_namespaced_cte_key(self):
        """NoiseFilter._table_short_name still correctly strips relative CTE keys.

        After the rel-path change the key is 'subdir/fact.sql::my_cte' instead of
        '/abs/path/fact.sql::my_cte'; the '::'- split still yields 'my_cte'.
        Guards sprint_postmortem_fixes.md §PR 3 Step 3.0 (A3).
        """
        from sqlcg.server.noise_filter import _table_short_name

        relative_key = "subdir/fact.sql::my_cte"
        short = _table_short_name(relative_key)
        assert short == "my_cte", (
            f"_table_short_name('{relative_key}') returned {short!r}; expected 'my_cte'."
        )

    def test_noise_filter_table_short_name_handles_relative_cte_key(self):
        """_table_short_name extracts the CTE name from a relative namespaced key.

        The '::' split in _table_short_name is format-agnostic: both
        '/abs/path/fact.sql::my_cte' (old) and 'subdir/fact.sql::my_cte' (new)
        yield 'my_cte'.  Verifies that the noise-filter helper still functions
        after the rel-path change.
        Guards sprint_postmortem_fixes.md §PR 3 Step 3.0 (A3).
        """
        from sqlcg.server.noise_filter import _table_short_name

        # Old absolute-path format (for reference)
        old_key = "/abs/path/fact.sql::my_cte"
        # New repo-relative format
        new_key = "subdir/fact.sql::my_cte"

        assert _table_short_name(old_key) == "my_cte", (
            f"_table_short_name on old absolute key {old_key!r} returned wrong result."
        )
        assert _table_short_name(new_key) == "my_cte", (
            f"_table_short_name on new relative key {new_key!r} must also return 'my_cte'."
        )
