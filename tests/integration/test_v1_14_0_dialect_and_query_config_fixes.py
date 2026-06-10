"""Failing acceptance tests for the v1.14.0 dialect & query-config fix batch.

Plan: plan/sprints/v1.14.0_dialect_and_query_config_fixes.md

Covers, per ticket:
  Fix 1 — `--dialect` defaults to "auto"; config-less repo resolves to
          snowflake; watch_cmd resolves "auto" before its first index_repo
          call and before WatchJobManager / SqlFileEventHandler construction.
  Fix 2 — `catalog load` folds schema names through the alias map before
          `table_qualified` is built; ddl precedence and dedup preserved.
  Fix 3 — `resolved_repo_root()` resolves `.sqlcg.toml` from the indexed
          repo root (persisted `Repo.path`), not bare cwd.
  Fix 4a — blindspot ranking excludes kind IN ('cte', 'derived').
  Fix 4b — analyze upstream/downstream emit a notice when noise-filtering
          removes every result.

Each test is named after its ticket (test_Fix1_*, test_Fix2_*, ...) so
`pytest -k Fix2` selects just that ticket's tests. Tests that depend on
symbols not yet introduced by the developer use try/except ImportError +
pytest.skip per plan-reviewer convention — they must not pass silently.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.core.schema import NodeLabel

# ---------------------------------------------------------------------------
# Fix 1 — dialect default "auto"; config-less -> snowflake; watch resolution
# ---------------------------------------------------------------------------


def test_Fix1_index_cmd_default_option_is_auto() -> None:
    """The --dialect option on `index` defaults to "auto", not None.

    Before the fix: `typer.Option(None, "--dialect", ...)` — running
    `sqlcg index <repo>` with no flag passes dialect=None straight through to
    the parser pool, which selects ANSI regardless of .sqlcg.toml.
    """
    # Inspect the typer.Option default directly via the function signature.
    import inspect

    from sqlcg.cli.commands.index import index_cmd

    sig = inspect.signature(index_cmd)
    dialect_param = sig.parameters["dialect"]
    default = dialect_param.default
    # typer.Option(...) returns an OptionInfo; its `.default` attribute holds
    # the actual default value passed as the first positional arg.
    resolved_default = getattr(default, "default", default)
    assert resolved_default == "auto", (
        f"Expected --dialect default to be 'auto' (Fix 1, Step 1.1), got "
        f"{resolved_default!r}. With None, `sqlcg index <repo>` silently uses "
        "the ANSI parser regardless of .sqlcg.toml."
    )


def test_Fix1_reindex_cmd_default_option_is_auto() -> None:
    """The --dialect option on `reindex` defaults to "auto", not None (Step 1.2)."""
    import inspect

    from sqlcg.cli.commands.reindex import reindex_cmd

    sig = inspect.signature(reindex_cmd)
    dialect_param = sig.parameters["dialect"]
    default = dialect_param.default
    resolved_default = getattr(default, "default", default)
    assert resolved_default == "auto", (
        f"Expected --dialect default to be 'auto' (Fix 1, Step 1.2), got {resolved_default!r}."
    )


def test_Fix1_watch_cmd_default_option_is_auto() -> None:
    """The --dialect option on `watch` defaults to "auto", not None (Step 1.3)."""
    import inspect

    from sqlcg.cli.commands.watch import watch_cmd

    sig = inspect.signature(watch_cmd)
    dialect_param = sig.parameters["dialect"]
    default = dialect_param.default
    resolved_default = getattr(default, "default", default)
    assert resolved_default == "auto", (
        f"Expected --dialect default to be 'auto' (Fix 1, Step 1.3), got "
        f"{resolved_default!r}. watch.py currently has NO 'auto' resolution at "
        "all — Step 1.3 must add it."
    )


def test_Fix1_watch_cmd_resolves_auto_before_use(tmp_path: Path) -> None:
    """watch_cmd resolves 'auto' to the .sqlcg.toml dialect before index_repo,
    WatchJobManager, and SqlFileEventHandler all receive it.

    Before the fix: watch.py has no "auto" resolution at all — "auto" (or
    None) flows straight into index_repo / WatchJobManager / SqlFileEventHandler,
    none of which understand the sentinel.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".sqlcg.toml").write_text('[sqlcg]\ndialect = "snowflake"\n')

    with (
        patch("sqlcg.cli.commands.watch.get_backend") as mock_get_backend,
        patch("sqlcg.cli.commands.watch.get_db_path") as mock_get_db_path,
        patch("sqlcg.cli.commands.watch.Indexer") as mock_indexer_class,
        patch("sqlcg.cli.commands.watch.WatchJobManager") as mock_job_manager_class,
        patch("sqlcg.cli.commands.watch.SqlFileEventHandler") as mock_handler_class,
        patch("sqlcg.cli.commands.watch.Observer") as mock_observer_class,
        patch("sqlcg.cli.commands.watch.load_ignore_spec", return_value=None),
    ):
        mock_backend = MagicMock()
        mock_backend.get_schema_version.return_value = mock_backend.get_schema_version.return_value
        mock_get_backend.return_value.__enter__.return_value = mock_backend
        mock_get_db_path.return_value = tmp_path / ".sqlcg"

        # Match SCHEMA_VERSION so watch_cmd doesn't bail early.
        from sqlcg.core.schema import SCHEMA_VERSION

        mock_backend.get_schema_version.return_value = SCHEMA_VERSION

        mock_indexer = MagicMock()
        mock_indexer_class.return_value = mock_indexer

        mock_observer = MagicMock()
        # is_alive() must eventually return False so the watch loop exits.
        mock_observer.is_alive.side_effect = [True, False]
        mock_observer_class.return_value = mock_observer

        try:
            from sqlcg.cli.commands.watch import watch_cmd

            watch_cmd(repo, dialect="auto")
        except TypeError as exc:
            pytest.skip(f"Fix 1 Step 1.3 not yet implemented (watch_cmd signature): {exc}")

        # 1. The initial index_repo call must receive the resolved dialect.
        assert mock_indexer.index_repo.called, "index_repo was never called"
        call_args = mock_indexer.index_repo.call_args
        resolved_dialect = (
            call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("dialect")
        )
        assert resolved_dialect == "snowflake", (
            f"Expected index_repo's dialect arg to be the resolved 'snowflake', "
            f"got {resolved_dialect!r}. watch_cmd must resolve 'auto' -> "
            "get_dialect(path) before calling index_repo (Fix 1, Step 1.3)."
        )

        # 2. WatchJobManager must receive the resolved dialect, not "auto".
        assert mock_job_manager_class.called, "WatchJobManager was never constructed"
        jm_args = mock_job_manager_class.call_args[0]
        assert "auto" not in jm_args, (
            f"WatchJobManager received the literal sentinel 'auto': {jm_args!r}"
        )

        # 3. SqlFileEventHandler must receive the resolved dialect, not "auto".
        assert mock_handler_class.called, "SqlFileEventHandler was never constructed"
        handler_kwargs = mock_handler_class.call_args.kwargs
        assert handler_kwargs.get("dialect") != "auto", (
            f"SqlFileEventHandler received dialect='auto' "
            f"(kwargs={handler_kwargs!r}). Must be resolved to 'snowflake'."
        )
        assert handler_kwargs.get("dialect") == "snowflake", (
            f"Expected SqlFileEventHandler dialect='snowflake', got "
            f"{handler_kwargs.get('dialect')!r}."
        )


# ---------------------------------------------------------------------------
# Fix 2 — catalog load applies schema aliases at load time
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


def test_Fix2_alias_folding_produces_canonical_nodes_no_phantom(tmp_path: Path, backend) -> None:
    """A CSV row for ba_tmp.foo.col1, folded via {'ba_tmp': 'ba'}, produces
    ba.foo / ba.foo.col1 and creates NO ba_tmp.foo phantom node.

    This is the headline measured bug: 2,907 phantom *_tmp table nodes on the
    DWH (~41% of all table nodes).
    """
    try:
        from sqlcg.cli.commands.catalog import apply_catalog_to_backend
    except ImportError:
        pytest.skip("catalog module not importable")

    csv_path = _catalog_csv(tmp_path, [("ba_tmp", "foo", "col1")])

    try:
        apply_catalog_to_backend(csv_path, backend, schema_aliases={"ba_tmp": "ba"})
    except TypeError:
        pytest.skip(
            "Fix 2 not yet implemented: apply_catalog_to_backend has no schema_aliases param"
        )

    tables = backend.run_read('SELECT qualified FROM "SqlTable"', {})
    qualified_names = {r["qualified"] for r in tables}

    assert "ba.foo" in qualified_names, (
        f"Expected folded table 'ba.foo' in SqlTable, got {qualified_names!r}"
    )
    assert "ba_tmp.foo" not in qualified_names, (
        f"Phantom table 'ba_tmp.foo' must NOT be created when an alias maps "
        f"ba_tmp -> ba. Got tables: {qualified_names!r}"
    )

    columns = backend.run_read('SELECT id FROM "SqlColumn"', {})
    column_ids = {r["id"] for r in columns}
    assert "ba.foo.col1" in column_ids, f"Expected 'ba.foo.col1' in SqlColumn, got {column_ids!r}"
    assert "ba_tmp.foo.col1" not in column_ids, (
        f"Phantom column 'ba_tmp.foo.col1' must NOT be created. Got: {column_ids!r}"
    )


def test_Fix2_ddl_sourced_column_survives_aliased_catalog_load(tmp_path: Path, backend) -> None:
    """A pre-seeded ddl-sourced ba.x column must NOT be downgraded to
    information_schema by a folded ba_tmp.x catalog row (D2.2 precedence).
    """
    try:
        from sqlcg.cli.commands.catalog import apply_catalog_to_backend
    except ImportError:
        pytest.skip("catalog module not importable")

    # Pre-seed a ddl-sourced ba.x table + column + HAS_COLUMN edge.
    backend.run_write(
        'INSERT INTO "SqlTable" (qualified, name, catalog, db, kind) VALUES (?, ?, ?, ?, ?)',
        {"qualified": "ba.x", "name": "x", "catalog": "ba", "db": "ba", "kind": "table"},
    )
    backend.run_write(
        'INSERT INTO "SqlColumn" (id, catalog, db, table_name, col_name, table_qualified) '
        "VALUES (?, ?, ?, ?, ?, ?)",
        {
            "id": "ba.x.col1",
            "catalog": "ba",
            "db": "ba",
            "table_name": "x",
            "col_name": "col1",
            "table_qualified": "ba.x",
        },
    )
    backend.run_write(
        'INSERT INTO "HAS_COLUMN" (src_key, dst_key, source) VALUES (?, ?, ?)',
        {"src_key": "ba.x", "dst_key": "ba.x.col1", "source": "ddl"},
    )

    csv_path = _catalog_csv(tmp_path, [("ba_tmp", "x", "col1")])

    try:
        apply_catalog_to_backend(csv_path, backend, schema_aliases={"ba_tmp": "ba"})
    except TypeError:
        pytest.skip(
            "Fix 2 not yet implemented: apply_catalog_to_backend has no schema_aliases param"
        )

    rows = backend.run_read(
        'SELECT source FROM "HAS_COLUMN" WHERE src_key = ? AND dst_key = ?',
        {"src_key": "ba.x", "dst_key": "ba.x.col1"},
    )
    assert rows, "Expected ba.x -> ba.x.col1 HAS_COLUMN row to still exist"
    assert rows[0]["source"] == "ddl", (
        f"DDL-sourced HAS_COLUMN row for ba.x.col1 must NOT be downgraded to "
        f"information_schema by a folded ba_tmp.col1 row. Got source="
        f"{rows[0]['source']!r}"
    )


def test_Fix2_zero_config_path_unchanged(tmp_path: Path, backend) -> None:
    """An empty/absent alias map behaves exactly as today: ba_tmp.x loads verbatim
    as ba_tmp.x (no folding).
    """
    try:
        from sqlcg.cli.commands.catalog import apply_catalog_to_backend
    except ImportError:
        pytest.skip("catalog module not importable")

    csv_path = _catalog_csv(tmp_path, [("ba_tmp", "foo", "col1")])

    # Call with no schema_aliases arg at all (must default to empty dict).
    apply_catalog_to_backend(csv_path, backend)

    tables = backend.run_read('SELECT qualified FROM "SqlTable"', {})
    qualified_names = {r["qualified"] for r in tables}
    assert "ba_tmp.foo" in qualified_names, (
        f"Zero-config path (no schema_aliases) must load verbatim as "
        f"'ba_tmp.foo'. Got: {qualified_names!r}"
    )


# ---------------------------------------------------------------------------
# Fix 3 — repo-anchored query-time config (resolved_repo_root)
# ---------------------------------------------------------------------------


def test_Fix3_resolved_repo_root_uses_repo_path_not_cwd(tmp_path: Path, backend) -> None:
    """resolved_repo_root() returns the persisted Repo.path, not Path.cwd().

    This is the core of Fix 3: query-time config resolution must anchor on the
    indexed repo root, not wherever the CLI happens to be invoked from.
    """
    try:
        from sqlcg.server.read_client import resolved_repo_root  # introduced by Fix 3
    except ImportError:
        pytest.skip("Fix 3 not yet implemented: resolved_repo_root not found in read_client")

    indexed_root = str(tmp_path / "indexed_repo")
    backend.upsert_node(
        NodeLabel.REPO,
        indexed_root,
        {"path": indexed_root, "name": "indexed_repo"},
    )

    def _routed(query: str, params: dict):
        return backend.run_read(query, params)

    with patch("sqlcg.server.read_client.run_read_routed", side_effect=_routed):
        root = resolved_repo_root()

    assert str(root) == indexed_root, (
        f"resolved_repo_root() must return the persisted Repo.path "
        f"({indexed_root!r}), got {root!r}. Falling back to cwd reintroduces "
        "the bug where queries from outside the indexed repo silently lose "
        ".sqlcg.toml config."
    )


def test_Fix3_resolved_repo_root_falls_back_to_cwd_when_no_repo_row(backend) -> None:
    """A graph with no Repo row falls back to Path.cwd() without raising."""
    try:
        from sqlcg.server.read_client import resolved_repo_root  # introduced by Fix 3
    except ImportError:
        pytest.skip("Fix 3 not yet implemented: resolved_repo_root not found in read_client")

    # backend has init_schema() but no Repo row.
    rows = backend.run_read('SELECT path FROM "Repo" LIMIT 1', {})
    assert rows == [], "Test setup invariant broken: Repo table should be empty"

    def _routed(query: str, params: dict):
        return backend.run_read(query, params)

    with patch("sqlcg.server.read_client.run_read_routed", side_effect=_routed):
        root = resolved_repo_root()

    assert root == Path.cwd(), (
        f"resolved_repo_root() with no Repo row must fall back to Path.cwd(), got {root!r}"
    )


def test_Fix3_analyze_upstream_uses_resolved_root_for_noise_filter(tmp_path: Path) -> None:
    """analyze upstream's NoiseFilter.from_config() call passes a resolved root,
    not bare cwd (D3.2 item 1, analyze.py:160).

    Regression for: "_bck clone tables appeared in traces when running from the
    sqlcg project dir and disappeared when running from the DWH repo dir."
    """
    import sqlcg.cli.commands.analyze as analyze_mod

    if not hasattr(analyze_mod, "resolved_repo_root") and "resolved_repo_root" not in dir(
        analyze_mod
    ):
        # Allow either a direct import or a module-level reference.
        try:
            from sqlcg.server.read_client import resolved_repo_root  # noqa: F401
        except ImportError:
            pytest.skip("Fix 3 not yet implemented: resolved_repo_root not found")

    with (
        patch("sqlcg.cli.commands.analyze.run_read_routed", return_value=[]),
        patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_from_config,
    ):
        mock_from_config.return_value.filter_nodes.return_value = ([], [])
        try:
            from sqlcg.cli.commands.analyze import _filter_column_results  # noqa: F401
        except ImportError:
            pass

        try:
            analyze_mod.upstream("a.b.c", depth=5, raw=False, include_intermediate=False)
        except Exception:
            # We only care whether from_config was called with a non-default
            # repo_root kwarg; downstream errors from the empty-results path
            # are not the subject of this assertion.
            pass

        assert mock_from_config.called, "NoiseFilter.from_config was never called"
        call_kwargs = mock_from_config.call_args.kwargs
        assert "repo_root" in call_kwargs and call_kwargs["repo_root"] is not None, (
            f"analyze upstream's NoiseFilter.from_config() call must pass an "
            f"explicit repo_root resolved via resolved_repo_root(), not rely on "
            f"the bare-cwd default. Got call kwargs: {call_kwargs!r}"
        )


# ---------------------------------------------------------------------------
# Fix 4a — blindspot ranking excludes kind IN ('cte', 'derived')
# ---------------------------------------------------------------------------


@pytest.fixture
def fix4a_backend():
    """A graph with bad edges to a CTE table and to a real table.

    - mydb.sch.real_table  kind='table', NO HAS_COLUMN -> blindspot (real gap)
    - mydb.sch.cte_node    kind='cte',   NO HAS_COLUMN -> must be excluded
    """
    b = DuckDBBackend(":memory:")
    b.init_schema()

    tables = [
        ("mydb.sch.real_table", "real_table", "table"),
        ("mydb.sch.cte_node", "cte_node", "cte"),
    ]
    for qualified, name, kind in tables:
        b.run_write(
            'INSERT INTO "SqlTable" (qualified, name, catalog, db, kind) VALUES (?, ?, ?, ?, ?)',
            {"qualified": qualified, "name": name, "catalog": "mydb", "db": "mydb", "kind": kind},
        )

    lineage_rows = [
        ("mydb.sch.stg.a", "mydb.sch.real_table.a", False, "/repo/etl/q1.sql:0"),
        ("mydb.sch.stg.b", "mydb.sch.cte_node.b", False, "/repo/etl/q1.sql:0"),
    ]
    for src, dst, inferred, query_id in lineage_rows:
        b.run_write(
            'INSERT INTO "COLUMN_LINEAGE" (src_key, dst_key, inferred_from_source_name, query_id)'
            " VALUES (?, ?, ?, ?)",
            {
                "src_key": src,
                "dst_key": dst,
                "inferred_from_source_name": inferred,
                "query_id": query_id,
            },
        )

    yield b
    b.close()


def test_Fix4a_blindspot_ranking_excludes_cte_and_derived(fix4a_backend) -> None:
    """top_blindspot_tables excludes kind IN ('cte','derived') dst tables,
    matching the scoped KPI's exclusion set.
    """
    from sqlcg.cli.coverage import collect_coverage

    def _routed(query: str, params: dict):
        return fix4a_backend.run_read(query, params)

    with patch("sqlcg.cli.coverage.run_read_routed", side_effect=_routed):
        coverage = collect_coverage()

    blindspot_names = {bt.table for bt in coverage.top_blindspot_tables}

    assert "mydb.sch.real_table" in blindspot_names, (
        f"Expected real blindspot table 'mydb.sch.real_table' in ranking, got {blindspot_names!r}"
    )
    assert "mydb.sch.cte_node" not in blindspot_names, (
        f"CTE-kind table 'mydb.sch.cte_node' must be excluded from the "
        f"blindspot ranking (Fix 4a). Got: {blindspot_names!r}. Before the fix, "
        "the entire top-10 was CTE structural nodes, burying real gaps."
    )


# ---------------------------------------------------------------------------
# Fix 4b — analyze upstream/downstream notice when noise-filter empties results
# ---------------------------------------------------------------------------


def test_Fix4b_all_noise_filtered_notice_fires(capsys) -> None:
    """When the canonical query returns rows but the noise filter removes all
    of them, analyze upstream prints an explanatory notice (not a silent empty
    table).
    """
    import sqlcg.cli.commands.analyze as analyze_mod

    fake_rows = [
        {"id": "ba_bck.foo.col1", "file_path": "/repo/x.sql", "start_line": 1},
    ]

    with (
        patch("sqlcg.cli.commands.analyze.run_read_routed", return_value=fake_rows),
        patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_from_config,
    ):
        nf = MagicMock()
        # Noise filter drops everything.
        nf.is_noise.return_value = True
        mock_from_config.return_value = nf

        # _filter_column_results uses nf under the hood — patch it directly to
        # guarantee post-filter-empty regardless of internal implementation,
        # while still letting pre_filter_count reflect fake_rows.
        with patch("sqlcg.cli.commands.analyze._filter_column_results", return_value=[]):
            try:
                analyze_mod.upstream("ba.foo.col1", depth=5, raw=False, include_intermediate=False)
            except AttributeError as exc:
                pytest.skip(f"Fix 4b not yet implemented: {exc}")

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "noise" in combined.lower() and (
        "filter" in combined.lower() or "removed" in combined.lower()
    ), (
        f"Expected a notice mentioning noise-filtering when all {len(fake_rows)} "
        f"result(s) were removed post-filter. Got output: {combined!r}"
    )


def test_Fix4b_notice_does_not_fire_when_results_survive(capsys) -> None:
    """The 'all noise-filtered' notice must NOT fire when surviving rows exist."""
    import sqlcg.cli.commands.analyze as analyze_mod

    fake_rows = [
        {"id": "ba.foo.col1", "file_path": "/repo/x.sql", "start_line": 1},
    ]

    with (
        patch("sqlcg.cli.commands.analyze.run_read_routed", return_value=fake_rows),
        patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_from_config,
        patch(
            "sqlcg.cli.commands.analyze._filter_column_results",
            return_value=fake_rows,
        ),
    ):
        nf = MagicMock()
        nf.is_noise.return_value = False
        mock_from_config.return_value = nf

        analyze_mod.upstream("ba.foo.col1", depth=5, raw=False, include_intermediate=False)

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "removed by the noise filter" not in combined, (
        f"The all-noise-filtered notice must not fire when results survive "
        f"post-filter. Got output: {combined!r}"
    )
