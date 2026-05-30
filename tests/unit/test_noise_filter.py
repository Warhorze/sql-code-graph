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


class TestNoiseFilterRegex:
    """Regex ignore layer — catch _bck markers anywhere in the name."""

    def test_regex_matches_bck_anywhere(self) -> None:
        """An unanchored `_bck` regex excludes names a `*_bck` glob would miss."""
        filter = NoiseFilter(patterns=["*_bck"], schema_aliases={}, ignore_regexes=["_bck"])

        # The anchored glob only catches suffix _bck...
        assert filter.is_noise("ba.table_bck") is True
        # ...but the regex also catches _bck when it is not the final token,
        # which the `*_bck` glob would miss.
        assert filter.is_noise("ba.table_bck_archive") is True
        assert filter.is_noise("ba.table_bck_20240101_old") is True

    def test_regex_does_not_overmatch(self) -> None:
        """A `_bck` regex must not flag an unrelated table."""
        filter = NoiseFilter(patterns=[], schema_aliases={}, ignore_regexes=["_bck"])
        assert filter.is_noise("ba.verkoopinfo") is False

    def test_regex_is_case_insensitive(self) -> None:
        """Regex layer matches case-insensitively like the other layers."""
        filter = NoiseFilter(patterns=[], schema_aliases={}, ignore_regexes=["_bck"])
        assert filter.is_noise("BA.TABLE_BCK_OLD") is True

    def test_invalid_regex_is_skipped_not_raised(self) -> None:
        """A malformed regex is dropped silently and never matches."""
        filter = NoiseFilter(patterns=[], schema_aliases={}, ignore_regexes=["[unclosed"])
        # Construction did not raise; the bad pattern simply matches nothing.
        assert filter.is_noise("ba.anything") is False

    def test_default_regexes_empty(self) -> None:
        """get_ignore_table_regexes defaults to [] when config is absent."""
        from sqlcg.core.config import get_ignore_table_regexes

        assert get_ignore_table_regexes(Path("/nonexistent")) == []

    def test_get_regexes_from_toml(self, tmp_path: Path) -> None:
        """get_ignore_table_regexes reads the configured list verbatim."""
        from sqlcg.core.config import get_ignore_table_regexes

        (tmp_path / ".sqlcg.toml").write_text(
            """
[sqlcg.noise_filter]
ignore_table_regexes = ["_bck", "_tmp_[0-9]{8}"]
"""
        )
        assert get_ignore_table_regexes(tmp_path) == ["_bck", "_tmp_[0-9]{8}"]

    def test_from_config_wires_regexes(self, tmp_path: Path) -> None:
        """from_config wires ignore_table_regexes into the NoiseFilter."""
        (tmp_path / ".sqlcg.toml").write_text(
            """
[sqlcg.noise_filter]
ignore_table_regexes = ["_bck"]
"""
        )
        filter = NoiseFilter.from_config(repo_root=tmp_path)
        assert filter.is_noise("ba.foo_bck_archive") is True
        assert filter.is_noise("ba.foo") is False


# ---------------------------------------------------------------------------
# PR-08 #27a — *_bck_* default glob catches mid-suffix variants
# ---------------------------------------------------------------------------


class TestBckStarDefaultGlob:
    """#27a: *_bck_* must be in the default pattern list."""

    def test_scenario_a_bck_star_catches_mid_suffix(self, tmp_path: "Path") -> None:
        """Scenario A: default config matches foo_bck_us39553.

        foo_bck_us39553 has an intermediate word after _bck_ — the old pattern
        *_bck_us only matched _bck_us as a suffix, missing _bck_us39553.
        The new *_bck_* pattern catches the whole class.
        """
        from sqlcg.server.noise_filter import NoiseFilter

        # from_config with no .sqlcg.toml → uses default_patterns
        nf = NoiseFilter.from_config(repo_root=tmp_path)
        assert nf.is_noise("ba.foo_bck_us39553") is True, (
            "*_bck_* default pattern must match foo_bck_us39553"
        )

    def test_scenario_b_bck_star_does_not_over_match_leading_bck(self, tmp_path: "Path") -> None:
        """Scenario B: *_bck_* does not match names that start with bck_.

        fnmatch('bck_tracker', '*_bck_*') is False because '*_bck_*' requires
        at least one character before _bck_ (the leading *_ anchors to a char).
        """
        import fnmatch

        assert fnmatch.fnmatch("bck_tracker", "*_bck_*") is False, (
            "fnmatch('bck_tracker', '*_bck_*') must be False: "
            "the pattern requires at least one char before _bck_"
        )

        from sqlcg.server.noise_filter import NoiseFilter

        nf = NoiseFilter.from_config(repo_root=tmp_path)
        assert nf.is_noise("ba.bck_tracker") is False, (
            "*_bck_* must not match 'bck_tracker' (name starts with bck_, not _bck_)"
        )


# ---------------------------------------------------------------------------
# PR-08 #27b — CLI analyze upstream respects NoiseFilter
# ---------------------------------------------------------------------------


class TestAnalyzeNoiseFilter:
    """#27b: analyze upstream/downstream applies NoiseFilter by default."""

    def test_scenario_c_analyze_upstream_excludes_noise_by_default(self) -> None:
        """Scenario C: backup table nodes are excluded from CLI output by default.

        Uses a mock backend returning a backup node; verifies that with default
        NoiseFilter the node is excluded, and with --raw it is returned.
        """
        from unittest.mock import MagicMock, patch

        from typer.testing import CliRunner

        from sqlcg.cli.main import app

        runner = CliRunner()
        backup_id = "ba.src_bck_us99.amount"
        normal_id = "ba.src.amount"

        backend = MagicMock()
        backend.__enter__ = MagicMock(return_value=backend)
        backend.__exit__ = MagicMock(return_value=False)
        backend.run_read = MagicMock(return_value=[{"id": backup_id}, {"id": normal_id}])

        with patch("sqlcg.cli.commands.analyze.get_backend", return_value=backend):
            # Default: noise filtered out
            result_default = runner.invoke(app, ["analyze", "upstream", "ba.fact.col"])

        assert result_default.exit_code == 0, (
            f"exit_code={result_default.exit_code}: {result_default.output}"
        )
        assert backup_id not in result_default.output, (
            f"Backup node {backup_id!r} must be excluded by default NoiseFilter. "
            f"Output: {result_default.output}"
        )
        assert normal_id in result_default.output, (
            f"Normal node {normal_id!r} must appear in output. Output: {result_default.output}"
        )

        with patch("sqlcg.cli.commands.analyze.get_backend", return_value=backend):
            # --raw: backup node restored
            result_raw = runner.invoke(app, ["analyze", "upstream", "ba.fact.col", "--raw"])

        assert result_raw.exit_code == 0
        assert backup_id in result_raw.output, (
            f"Backup node {backup_id!r} must appear with --raw. Output: {result_raw.output}"
        )
