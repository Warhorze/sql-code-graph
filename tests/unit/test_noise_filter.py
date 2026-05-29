"""Unit tests for the NoiseFilter class."""

from pathlib import Path

from sqlcg.server.noise_filter import NoiseFilter


class TestNoiseFilterBackupPatternMatch:
    """Scenario A — backup pattern match."""

    def test_backup_pattern_suffix_bck(self) -> None:
        """Test is_noise returns True for *_bck pattern."""
        filter = NoiseFilter(patterns=["*_bck", "*_bck_[0-9]*"], schema_aliases={})
        assert filter.is_noise("ba.wtfe_verkoopinfo_bck") is True

    def test_backup_pattern_dated(self) -> None:
        """Test is_noise returns True for *_bck_[0-9]* pattern."""
        filter = NoiseFilter(patterns=["*_bck", "*_bck_[0-9]*"], schema_aliases={})
        assert filter.is_noise("ba.wtfe_verkoopinfo_bck_20240101") is True

    def test_non_backup_table(self) -> None:
        """Test is_noise returns False for regular table."""
        filter = NoiseFilter(patterns=["*_bck", "*_bck_[0-9]*"], schema_aliases={})
        assert filter.is_noise("ba.wtfe_verkoopinfo") is False


class TestNoiseFilterSchemaAliasCanonical:
    """Scenario B — schema-alias canonical form."""

    def test_canonical_unchanged_no_alias(self) -> None:
        """Test canonical returns input unchanged when no alias matches."""
        filter = NoiseFilter(patterns=[], schema_aliases={"ia_analytics": "ba"})
        result = filter.canonical("ia_analytics.ba_wtfe_verkoopinfo")
        assert result == "ia_analytics.ba_wtfe_verkoopinfo"

    def test_canonical_unmatched_schema(self) -> None:
        """Test canonical returns input unchanged for unmatched schema."""
        filter = NoiseFilter(patterns=[], schema_aliases={"ia_analytics": "ba"})
        result = filter.canonical("ia_analytics.x")
        assert result == "ia_analytics.x"


class TestNoiseFilterFilterNodes:
    """Scenario C — filter_nodes excludes noise and reports it."""

    def test_filter_nodes_separates_noise_and_kept(self) -> None:
        """Test filter_nodes returns two lists: kept and excluded noise."""
        filter = NoiseFilter(
            patterns=["*_bck"],
            schema_aliases={"ia_analytics": "ba"},
        )
        nodes = ["ba.dim_table", "ba.dim_table_bck", "ia_analytics.ba_dim_table"]
        kept, excluded = filter.filter_nodes(nodes)

        # Verify kept contains the non-noise table
        assert "ba.dim_table" in kept
        assert "ia_analytics.ba_dim_table" in kept

        # Verify excluded contains the noise table
        assert "ba.dim_table_bck" in excluded

    def test_filter_nodes_returns_tuple(self) -> None:
        """Test filter_nodes returns a tuple of two lists."""
        filter = NoiseFilter(patterns=["*_bck"], schema_aliases={})
        result = filter.filter_nodes(["ba.table", "ba.table_bck"])

        assert isinstance(result, tuple)
        assert len(result) == 2
        kept, excluded = result
        assert isinstance(kept, list)
        assert isinstance(excluded, list)

    def test_filter_nodes_empty_input(self) -> None:
        """Test filter_nodes with empty input returns empty lists."""
        filter = NoiseFilter(patterns=["*_bck"], schema_aliases={})
        kept, excluded = filter.filter_nodes([])
        assert kept == []
        assert excluded == []


class TestNoiseFilterDefaultPatterns:
    """Scenario D — default patterns applied when config absent."""

    def test_default_patterns_nonexistent_path(self) -> None:
        """Test get_noise_filter_patterns returns defaults for nonexistent path."""
        from sqlcg.core.config import get_noise_filter_patterns

        result = get_noise_filter_patterns(Path("/nonexistent"))

        # Assert length >= 3 and contains expected patterns
        assert len(result) >= 3
        assert "*_bck" in result
        assert "*_bck_us" in result
        assert "*_bck_[0-9]*" in result

    def test_default_patterns_content(self) -> None:
        """Test default patterns include expected backup suffixes."""
        from sqlcg.core.config import get_noise_filter_patterns

        result = get_noise_filter_patterns(Path("/nonexistent"))

        # All default patterns should be strings
        assert all(isinstance(p, str) for p in result)

        # All should match backup naming conventions
        assert all("_bck" in p or "_backup" in p for p in result)


class TestNoiseFilterFromConfig:
    """Test NoiseFilter.from_config classmethod."""

    def test_from_config_none_repo_root(self, tmp_path: Path) -> None:
        """Test from_config with None repo_root uses cwd."""
        # This test just verifies the method exists and returns a NoiseFilter
        # It doesn't need to change cwd
        filter = NoiseFilter.from_config(repo_root=None)
        assert isinstance(filter, NoiseFilter)
        assert isinstance(filter.patterns, list)
        assert isinstance(filter.schema_aliases, dict)

    def test_from_config_with_repo_root(self, tmp_path: Path) -> None:
        """Test from_config with explicit repo_root."""
        # Create a temporary .sqlcg.toml
        config_file = tmp_path / ".sqlcg.toml"
        config_file.write_text(
            """
[sqlcg.noise_filter]
ignore_table_patterns = ["*_tmp", "*_staging"]

[sqlcg.schema_aliases]
staging_schema = "prod_schema"
"""
        )

        filter = NoiseFilter.from_config(repo_root=tmp_path)

        assert isinstance(filter, NoiseFilter)
        assert "*_tmp" in filter.patterns
        assert "*_staging" in filter.patterns
        assert filter.schema_aliases.get("staging_schema") == "prod_schema"

    def test_from_config_defaults_when_missing(self, tmp_path: Path) -> None:
        """Test from_config uses defaults when .sqlcg.toml is absent."""
        # tmp_path has no .sqlcg.toml, so should use defaults
        filter = NoiseFilter.from_config(repo_root=tmp_path)

        # Should have default patterns
        assert "*_bck" in filter.patterns
        assert "*_backup" in filter.patterns


class TestNoiseFilterIgnoredTables:
    """Scenario E — explicit ignored_tables exact-name match (control/delta tables)."""

    def test_ignored_table_exact_match(self) -> None:
        """A qualified name in ignored_tables is noise even without a glob match."""
        filter = NoiseFilter(
            patterns=["*_bck"],
            schema_aliases={},
            ignored_tables=["ma.rtetl_delta"],
        )
        assert filter.is_noise("ma.rtetl_delta") is True
        # A non-listed table that matches no glob stays clean.
        assert filter.is_noise("ba.wtfe_verkoopinfo") is False

    def test_ignored_table_case_insensitive(self) -> None:
        """ignored_tables matching is case-insensitive."""
        filter = NoiseFilter(
            patterns=[],
            schema_aliases={},
            ignored_tables=["ma.rtetl_delta"],
        )
        assert filter.is_noise("MA.RTETL_DELTA") is True

    def test_ignored_table_partial_name_not_matched(self) -> None:
        """ignored_tables is exact-match, not substring/prefix."""
        filter = NoiseFilter(
            patterns=[],
            schema_aliases={},
            ignored_tables=["ma.rtetl_delta"],
        )
        assert filter.is_noise("ma.rtetl_delta_history") is False

    def test_filter_nodes_drops_ignored_table(self) -> None:
        """filter_nodes routes an ignored table into excluded."""
        filter = NoiseFilter(
            patterns=["*_bck"],
            schema_aliases={},
            ignored_tables=["ma.rtetl_delta"],
        )
        kept, excluded = filter.filter_nodes(["ba.mart", "ma.rtetl_delta", "ba.mart_bck"])
        assert kept == ["ba.mart"]
        assert "ma.rtetl_delta" in excluded
        assert "ba.mart_bck" in excluded

    def test_get_ignored_tables_default_empty(self) -> None:
        """get_ignored_tables defaults to [] when config is absent."""
        from sqlcg.core.config import get_ignored_tables

        assert get_ignored_tables(Path("/nonexistent")) == []

    def test_get_ignored_tables_from_toml(self, tmp_path: Path) -> None:
        """get_ignored_tables reads and lowercases the configured list."""
        from sqlcg.core.config import get_ignored_tables

        (tmp_path / ".sqlcg.toml").write_text(
            """
[sqlcg.noise_filter]
ignored_tables = ["MA.RTETL_DELTA", "ctl.Load_Log"]
"""
        )
        result = get_ignored_tables(tmp_path)
        assert result == ["ma.rtetl_delta", "ctl.load_log"]

    def test_from_config_loads_ignored_tables(self, tmp_path: Path) -> None:
        """from_config wires ignored_tables into the NoiseFilter."""
        (tmp_path / ".sqlcg.toml").write_text(
            """
[sqlcg.noise_filter]
ignored_tables = ["ma.rtetl_delta"]
"""
        )
        filter = NoiseFilter.from_config(repo_root=tmp_path)
        assert filter.is_noise("ma.rtetl_delta") is True


class TestNoiseFilterIntegration:
    """Integration tests combining multiple features."""

    def test_is_noise_with_various_patterns(self) -> None:
        """Test is_noise with a realistic set of patterns."""
        patterns = [
            "*_bck",
            "*_bck_us",
            "*_bck_[0-9]*",
            "*_backup",
            "*_backup_[0-9]*",
        ]
        filter = NoiseFilter(patterns=patterns, schema_aliases={})

        # Should match
        assert filter.is_noise("ba.table_bck") is True
        assert filter.is_noise("ba.table_bck_us") is True
        assert filter.is_noise("ba.table_bck_20240101") is True
        assert filter.is_noise("ba.table_backup") is True
        assert filter.is_noise("ba.table_backup_001") is True

        # Should not match
        assert filter.is_noise("ba.table") is False
        assert (
            filter.is_noise("ba.table_backup_copy") is False
        )  # Pattern is _backup_[0-9]*, not _backup_*

    def test_filter_nodes_with_realistic_data(self) -> None:
        """Test filter_nodes with a realistic mixed dataset."""
        patterns = ["*_bck", "*_bck_[0-9]*"]
        filter = NoiseFilter(patterns=patterns, schema_aliases={})

        nodes = [
            "ba.source_table",
            "ba.source_table_bck",
            "ba.etl_table",
            "ba.etl_table_bck_20240115",
            "ia_analytics.mart",
        ]

        kept, excluded = filter.filter_nodes(nodes)

        # Verify correct separation
        assert len(kept) == 3
        assert len(excluded) == 2
        assert "ba.source_table" in kept
        assert "ba.etl_table" in kept
        assert "ia_analytics.mart" in kept
        assert "ba.source_table_bck" in excluded
        assert "ba.etl_table_bck_20240115" in excluded
