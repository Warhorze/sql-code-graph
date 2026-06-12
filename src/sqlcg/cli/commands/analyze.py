"""Analyze command for lineage analysis."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from sqlcg.core.queries import GET_TABLE_EXTERNAL_CONSUMERS_QUERY
from sqlcg.server.read_client import resolved_repo_root, run_read_routed

if TYPE_CHECKING:
    from sqlcg.server.noise_filter import NoiseFilter

app = typer.Typer(help="Lineage analysis")
console = Console()


def _upstream_sql(depth: int, include_intermediate: bool) -> str:
    """Build the upstream recursive-CTE SQL query.

    Traverses COLUMN_LINEAGE edges from dst→src (upstream direction).
    Applies kind-filter (LEFT JOIN SqlTable) to exclude CTE/derived intermediates
    unless include_intermediate=True.  The kind-guard mirrors the Cypher
    OPTIONAL MATCH + WITH … WHERE t.kind IS NULL OR t.kind IN ['table','external']
    semantics (#38/#40/19.2).
    """
    kind_filter = (
        ""
        if include_intermediate
        else (
            '  LEFT JOIN "SqlTable" t ON t.qualified = dr.table_qualified\n'
            "  WHERE t.kind IS NULL OR t.kind IN ('table', 'external')\n"
        )
    )
    return f"""
WITH RECURSIVE reach(id, table_qualified, depth, path) AS (
  SELECT
    cl.src_key AS id,
    c_src.table_qualified,
    1 AS depth,
    ARRAY[cl.dst_key, cl.src_key] AS path
  FROM "COLUMN_LINEAGE" cl
  JOIN "SqlColumn" c_src ON c_src.id = cl.src_key
  WHERE cl.dst_key = ?
  UNION ALL
  SELECT
    cl2.src_key,
    c2.table_qualified,
    reach.depth + 1,
    array_append(reach.path, cl2.src_key)
  FROM reach
  JOIN "COLUMN_LINEAGE" cl2 ON cl2.dst_key = reach.id
  JOIN "SqlColumn" c2 ON c2.id = cl2.src_key
  WHERE reach.depth < {depth}
    AND NOT cl2.src_key = ANY(reach.path)
),
distinct_reach AS (
  SELECT DISTINCT id, table_qualified FROM reach
)
SELECT
  dr.id,
  dr.table_qualified,
  min(q.file_path) AS file,
  min(q.start_line) AS line
FROM distinct_reach dr
LEFT JOIN "COLUMN_LINEAGE" src_edge ON src_edge.src_key = dr.id
LEFT JOIN "SqlQuery" q ON q.id = src_edge.query_id
{kind_filter}GROUP BY dr.id, dr.table_qualified
LIMIT 100
"""


def _downstream_sql(depth: int, include_intermediate: bool) -> str:
    """Build the downstream recursive-CTE SQL query.

    Traverses COLUMN_LINEAGE edges from src→dst (downstream direction).
    Kind-filter mirrors upstream: LEFT JOIN SqlTable + IS NULL OR 'table'/'external'.
    """
    kind_filter = (
        ""
        if include_intermediate
        else (
            '  LEFT JOIN "SqlTable" t ON t.qualified = dr.table_qualified\n'
            "  WHERE t.kind IS NULL OR t.kind IN ('table', 'external')\n"
        )
    )
    return f"""
WITH RECURSIVE reach(id, table_qualified, depth, path) AS (
  SELECT
    cl.dst_key AS id,
    c_dst.table_qualified,
    1 AS depth,
    ARRAY[cl.src_key, cl.dst_key] AS path
  FROM "COLUMN_LINEAGE" cl
  JOIN "SqlColumn" c_dst ON c_dst.id = cl.dst_key
  WHERE cl.src_key = ?
  UNION ALL
  SELECT
    cl2.dst_key,
    c2.table_qualified,
    reach.depth + 1,
    array_append(reach.path, cl2.dst_key)
  FROM reach
  JOIN "COLUMN_LINEAGE" cl2 ON cl2.src_key = reach.id
  JOIN "SqlColumn" c2 ON c2.id = cl2.dst_key
  WHERE reach.depth < {depth}
    AND NOT cl2.dst_key = ANY(reach.path)
),
distinct_reach AS (
  SELECT DISTINCT id, table_qualified FROM reach
)
SELECT
  dr.id,
  dr.table_qualified,
  min(q.file_path) AS file,
  min(q.start_line) AS line
FROM distinct_reach dr
LEFT JOIN "COLUMN_LINEAGE" dst_edge ON dst_edge.dst_key = dr.id
LEFT JOIN "SqlQuery" q ON q.id = dst_edge.query_id
{kind_filter}GROUP BY dr.id, dr.table_qualified
LIMIT 100
"""


@app.command("upstream")
def upstream(  # noqa: B008
    ref: str = typer.Argument(..., help="Column reference"),  # noqa: B008
    depth: int = typer.Option(5, "--depth", help="Maximum traversal depth"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
    include_intermediate: bool = typer.Option(  # noqa: B008
        False, "--include-intermediate", help="Include CTE/derived intermediate nodes"
    ),
) -> None:
    """Trace upstream column lineage."""
    if depth < 1 or depth > 100:
        console.print("[red]Error: --depth must be between 1 and 100[/red]")
        raise typer.Exit(1)

    ref = ref.lower()  # graph keys are lowercased at index time (C2 normalization)
    sql = _upstream_sql(depth, include_intermediate)
    results = run_read_routed(sql, {"ref": ref})
    # Bare-name fallback: PR 3 namespaced CTE keys contain "::" and dots from the
    # file path but have no schema prefix — exclude them to avoid a no-op retry.
    if not results and "::" not in ref and len(ref.split(".")) >= 3:
        bare = _bare_ref(ref)
        fallback_results = run_read_routed(sql, {"bare": bare})
        if fallback_results:
            console.print(
                f"[yellow]Hint:[/yellow] No results for '{ref}'. "
                f"Found {len(fallback_results)} edge(s) under bare name '{bare}'. "
                "The INSERT target may have been indexed without a schema prefix. "
                "Multiple tables with the same unqualified name in different schemas "
                "would all match — re-index with an explicit schema for precise results."
            )
            results = fallback_results
    if not raw:
        from sqlcg.server.noise_filter import NoiseFilter

        pre_filter_count = len(results)
        nf = NoiseFilter.from_config(repo_root=resolved_repo_root())
        results = _filter_column_results(results, nf)
        _print_noise_filtered_notice(pre_filter_count, len(results))
    _print_table(_add_file_line_col(results), ["id", "file:line"])


@app.command("downstream")
def downstream(  # noqa: B008
    ref: str = typer.Argument(..., help="Column reference"),  # noqa: B008
    depth: int = typer.Option(5, "--depth", help="Maximum traversal depth"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
    include_intermediate: bool = typer.Option(  # noqa: B008
        False, "--include-intermediate", help="Include CTE/derived intermediate nodes"
    ),
) -> None:
    """Trace downstream column lineage."""
    if depth < 1 or depth > 100:
        console.print("[red]Error: --depth must be between 1 and 100[/red]")
        raise typer.Exit(1)

    ref = ref.lower()  # graph keys are lowercased at index time (C2 normalization)
    sql = _downstream_sql(depth, include_intermediate)
    results = run_read_routed(sql, {"ref": ref})
    # Bare-name fallback: PR 3 namespaced CTE keys contain "::" and dots from the
    # file path but have no schema prefix — exclude them to avoid a no-op retry.
    if not results and "::" not in ref and len(ref.split(".")) >= 3:
        bare = _bare_ref(ref)
        fallback_results = run_read_routed(sql, {"bare": bare})
        if fallback_results:
            console.print(
                f"[yellow]Hint:[/yellow] No results for '{ref}'. "
                f"Found {len(fallback_results)} edge(s) under bare name '{bare}'. "
                "The INSERT target may have been indexed without a schema prefix. "
                "Multiple tables with the same unqualified name in different schemas "
                "would all match — re-index with an explicit schema for precise results."
            )
            results = fallback_results
    if not raw:
        from sqlcg.server.noise_filter import NoiseFilter

        pre_filter_count = len(results)
        nf = NoiseFilter.from_config(repo_root=resolved_repo_root())
        results = _filter_column_results(results, nf)
        _print_noise_filtered_notice(pre_filter_count, len(results))
    _print_table(_add_file_line_col(results), ["id", "file:line"])

    # Append external consumer rows for terminal tables (scalar query, one per terminal).
    terminal_tables: set[str] = set()
    for r in results:
        tbl = _col_id_to_table(r["id"])
        if tbl:
            terminal_tables.add(tbl)
    root_parts = ref.rsplit(".", 1)
    if len(root_parts) == 2:
        terminal_tables.add(root_parts[0])
    consumer_rows: list[dict] = []
    for tbl in sorted(terminal_tables):
        rows_ec = run_read_routed(
            GET_TABLE_EXTERNAL_CONSUMERS_QUERY,
            {"table_qualified": tbl},
        )
        for ec in rows_ec:
            consumer_rows.append(
                {"id": f"[external] {ec['name']} ({ec['consumer_type']})", "file:line": ""}
            )
    if consumer_rows:
        _print_table(consumer_rows, ["id", "file:line"])


@app.command("impact")
def impact(  # noqa: B008
    table: str = typer.Argument(..., help="Table name to analyze"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
) -> None:
    """Show all queries impacted by a table."""
    results = run_read_routed(
        "SELECT DISTINCT q.id AS id, q.kind AS kind, q.target_table AS target"
        ' FROM "SqlTable" t'
        ' JOIN "SELECTS_FROM" sf ON sf.dst_key = t.qualified'
        ' JOIN "SqlQuery" q ON q.id = sf.src_key'
        " WHERE t.qualified = ? LIMIT 100",
        {"t": table},
    )
    if not raw:
        from sqlcg.server.noise_filter import NoiseFilter

        nf = NoiseFilter.from_config(repo_root=resolved_repo_root())
        results = [r for r in results if not nf.is_noise(r.get("target", ""))]
    _print_table(results, ["id", "kind"])


@app.command("failures")
def failures(
    cause: str | None = typer.Option(  # noqa: B008
        None,
        "--cause",
        help=(
            "Filter by E-code bucket. Valid values: "
            "timeout, E8, E3, E2, E5, E1, qualify_failed, func_fallback, pure_ddl_skip"
        ),
    ),
    limit: int = typer.Option(100, "--limit", help="Maximum rows to return"),  # noqa: B008
) -> None:
    """List files that failed to parse, with their dominant cause (E-code bucket)."""
    if cause is not None:
        rows = run_read_routed(
            'SELECT path, parse_cause AS cause FROM "File"'
            " WHERE parse_failed = true AND parse_cause = ?"
            f" ORDER BY parse_cause LIMIT {limit}",
            {"cause": cause},
        )
    else:
        rows = run_read_routed(
            'SELECT path, parse_cause AS cause FROM "File"'
            f" WHERE parse_failed = true ORDER BY parse_cause LIMIT {limit}",
            {},
        )
    _print_table(rows, ["path", "cause"])


@app.command("unused")
def unused(
    threshold: int = typer.Option(0, "--threshold", help="Minimum reference count threshold"),
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
) -> None:
    """Find tables with no detected read of any kind.

    A table is considered USED if any of the following signals fires:
    (D1) a direct SELECTS_FROM read, (D2) a STAR_SOURCE (SELECT *) read, or
    (D3) a value flows out of one of its columns via COLUMN_LINEAGE (captures
    CTE-wrapped derived reads, the dominant corpus pattern).

    KNOWN GAP: a table read ONLY inside a CTE without deriving any column
    (a pure-gating read — e.g. used only as a join filter, no value selected
    from it) is NOT detected by any of the three signals. Such a table will
    still appear as "unused" even if it is genuinely needed.
    """
    results = run_read_routed(
        'SELECT DISTINCT qualified FROM "SqlTable"'
        " WHERE qualified NOT IN ("
        '    SELECT DISTINCT dst_key AS table_qualified FROM "SELECTS_FROM"'
        "    UNION"
        '    SELECT DISTINCT dst_key AS table_qualified FROM "STAR_SOURCE"'
        "    UNION"
        "    SELECT DISTINCT src.table_qualified AS table_qualified"
        '    FROM "COLUMN_LINEAGE" cl'
        '    JOIN "SqlColumn" src ON src.id = cl.src_key'
        "    WHERE src.table_qualified IS NOT NULL AND src.table_qualified <> ''"
        ")"
        " LIMIT 100",
        {},
    )
    if not raw:
        from sqlcg.server.noise_filter import NoiseFilter

        nf = NoiseFilter.from_config(repo_root=resolved_repo_root())
        results = [r for r in results if not nf.is_noise(r["qualified"])]
    _print_table(results, ["qualified"])


def _bare_ref(ref: str) -> str:
    """Strip schema prefix from a ref string, keeping table.column.

    Lowercases defensively so this is safe to call even if the caller did not
    first fold the ref — graph keys are lowercased at index time (C2 normalization).

    PR 3 (sprint_lineage_identity_and_session_context.md §PR 3): namespaced CTE
    keys carry ``::`` and dots from the file path but have no schema prefix;
    return them unchanged.
    """
    ref = ref.lower()
    if "::" in ref:
        return ref  # namespaced CTE key — no schema prefix to strip
    parts = ref.split(".")
    if len(parts) >= 3:
        return ".".join(parts[1:])
    return ref


def _col_id_to_table(col_id: str) -> str:
    """Extract the table-qualified part from a column ID."""
    parts = col_id.rsplit(".", 1)
    return parts[0] if len(parts) == 2 else col_id


def _filter_column_results(
    results: list[dict],
    nf: NoiseFilter,  # type: ignore[name-defined]
) -> list[dict]:
    """Filter column-ID result rows by NoiseFilter."""
    return [r for r in results if not nf.is_noise(_col_id_to_table(r["id"]))]


def _print_noise_filtered_notice(pre_filter_count: int, post_filter_count: int) -> None:
    """Print a notice when the noise filter removed every result row.

    Shared by `upstream` and `downstream` (Fix 4b) to avoid drift. Fires only
    when the canonical/fallback query returned rows but the noise filter
    dropped all of them — a genuinely empty trace (pre_filter_count == 0)
    keeps the existing "No results" behaviour with no spurious notice.
    """
    if pre_filter_count > 0 and post_filter_count == 0:
        console.print(
            f"[yellow]Notice:[/yellow] All {pre_filter_count} edge(s) were "
            "removed by the noise filter (run with --raw to see them)."
        )


def _add_file_line_col(rows: list[dict]) -> list[dict]:
    """Add a 'file:line' composite column from 'file' and 'line' fields."""
    result = []
    for row in rows:
        new_row = dict(row)
        file = row.get("file")
        line = row.get("line")
        if file and line:
            new_row["file:line"] = f"{file}:{line}"
        else:
            new_row["file:line"] = "?"
        result.append(new_row)
    return result


def _print_table(rows: list[dict], columns: list[str]) -> None:
    """Print results as a Rich table."""
    if not rows:
        console.print("[yellow]No results[/yellow]")
        return
    t = Table(*columns)
    for row in rows:
        t.add_row(*[str(row.get(c, "")) for c in columns])
    console.print(t)


# ---------------------------------------------------------------------------
# empty-impact command (PR 1 — v1.22.0)
# ---------------------------------------------------------------------------

_ROW_EMPTY_LABEL = (
    "tables that may go fully empty — direct gating-join reads + value-derived tables; "
    "CTE-wrapped pure-gating reads are NOT detected (known gap); depends on join type."
)


@app.command("empty-impact")
def empty_impact(  # noqa: B008
    tables: list[str] = typer.Argument(..., help="Qualified table name(s) to analyze"),  # noqa: B008
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering"),  # noqa: B008
    max_depth: int | None = typer.Option(  # noqa: B008
        None,
        "--max-depth",
        help="Maximum traversal depth (1-100). Default: unbounded.",
    ),
) -> None:
    """Show downstream blast radius when named table(s) are empty.

    Returns TWO views: View 2 (PRIMARY — column-lineage refinement) lists
    downstream columns/tables that lose their source values. View 1 (SUPPLEMENT)
    adds tables that row-empty via a direct gating join. See result for caveats.
    """
    # N2 — validate max_depth.
    if max_depth is not None and (max_depth < 1 or max_depth > 100):
        console.print("[red]Error: --max-depth must be between 1 and 100[/red]")
        raise typer.Exit(1)

    from sqlcg.core.config import get_backend
    from sqlcg.server.tools import _compute_empty_propagation

    # W2 / O1 — open backend directly; no existing analyze command holds a handle.
    with get_backend(read_only=True) as db:
        result = _compute_empty_propagation(db, list(tables), max_depth)

    # --raw: when raw, append noise-excluded tables to the display lists.
    # The engine always returns noise_excluded separately; --raw re-includes them.
    row_tables = result.row_empty_tables
    value_cols = result.value_empty_columns
    val_tables = result.value_affected_tables
    prop_order = result.propagation_order
    if raw:
        row_tables = list(row_tables) + result.noise_excluded
        val_tables = list(val_tables) + result.noise_excluded

    # Render View 2 (PRIMARY) first.
    console.print()
    console.print("[bold green]View 2 — Value derivation (PRIMARY)[/bold green]")
    console.print(
        "  Downstream columns/tables whose VALUES are transitively derived from the source."
    )
    console.print()

    if val_tables:
        fully_set = set(result.value_fully_empty_tables)
        partially_set = set(result.value_partially_empty_tables)
        tbl = Table("table", "value-empty / total columns", "full|partial")
        for t in val_tables:
            ve_count = sum(1 for c in value_cols if c.rsplit(".", 1)[0] == t)
            marker = "full" if t in fully_set else ("partial" if t in partially_set else "?")
            tbl.add_row(t, str(ve_count), marker)
        console.print(tbl)
        n_col = len(value_cols)
        n_tbl = len(val_tables)
        console.print(f"  {n_col} value-empty column(s) across {n_tbl} table(s).")
    else:
        console.print("  [yellow]No value-derived downstream tables found.[/yellow]")

    console.print()

    # Render View 1 (SUPPLEMENT) second.
    console.print("[bold yellow]View 1 — Row reachability (SUPPLEMENT)[/bold yellow]")
    console.print(f"  {_ROW_EMPTY_LABEL}")
    console.print()

    if row_tables:
        tbl2 = Table("table")
        for t in row_tables:
            tbl2.add_row(t)
        console.print(tbl2)
    else:
        console.print("  [yellow]No row-reachable downstream tables found.[/yellow]")

    console.print()

    # Propagation order.
    if prop_order:
        console.print("[bold]Propagation order (closest-to-source first):[/bold]")
        for i, t in enumerate(prop_order, 1):
            console.print(f"  {i}. {t}")
        console.print()

    # Diagnostics.
    if result.hint:
        console.print(f"[yellow]Hint:[/yellow] {result.hint}")
    if result.truncated:
        console.print(
            "[yellow]Warning:[/yellow] Traversal hit the 50k-node safety cap; "
            "results may be incomplete."
        )
    if result.noise_excluded and not raw:
        console.print(
            f"[dim]{len(result.noise_excluded)} table(s) excluded as backup/noise/synthetic "
            "(use --raw to include them).[/dim]"
        )


# ---------------------------------------------------------------------------
# pr-impact command (PR 2 — v1.23.0)
# ---------------------------------------------------------------------------

_PR_IMPACT_RUNTIME_BLIND_CAVEAT = (
    "NOTE: This tool detects CODE REGRESSIONS only (SQL that produced a table was "
    "removed/renamed/broken). It CANNOT detect runtime emptiness — a job that runs "
    "and produces 0 rows while the SQL is unchanged will NOT be flagged here."
)


@app.command("pr-impact")
def pr_impact(  # noqa: B008
    base: str = typer.Option(  # noqa: B008
        ..., "--base", help="Base git ref (branch, tag, or SHA) the graph is indexed at"
    ),
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering"),  # noqa: B008
    max_depth: int | None = typer.Option(  # noqa: B008
        None,
        "--max-depth",
        help="Maximum traversal depth (1-100). Default: unbounded.",
    ),
) -> None:
    """Detect tables that lose their producer between base ref and HEAD.

    Resyncs the graph from ``base`` to HEAD (same as ``sqlcg reindex``), then
    shows which tables had their producer SQL removed/modified away, their
    attribution, and the PR 1 two-view blast radius for genuine losses.

    CODE REGRESSION DETECTION ONLY — see printed caveat for the runtime-blind
    limitation.  Rename handling: tables whose producer was renamed (both
    column-set AND consumer Jaccard ≥ 0.6) are shown under "verify consumers
    updated" and produce no blast-radius warning.
    """
    # N2 — validate max_depth.
    if max_depth is not None and (max_depth < 1 or max_depth > 100):
        console.print("[red]Error: --max-depth must be between 1 and 100[/red]")
        raise typer.Exit(1)

    # W2 / O1 — get_pr_impact manages the backend internally (it calls _open_backend).
    # The resync step requires a writable backend, so we do not open read_only here.
    from sqlcg.server.tools import get_pr_impact as _get_pr_impact

    result = _get_pr_impact(base_ref=base, max_depth=max_depth)

    # --- Render ---
    console.print()
    console.print(f"[bold]PR Impact: [cyan]{base}[/cyan] → HEAD[/bold]")
    console.print()

    # Runtime-blind caveat (prominently at the top).
    console.print(f"[yellow]{_PR_IMPACT_RUNTIME_BLIND_CAVEAT}[/yellow]")
    console.print()

    # Hint (unresolvable ref / mismatch / no changes).
    if result.hint:
        console.print(f"[yellow]Hint:[/yellow] {result.hint}")
        console.print()

    if result.base_sha is None or result.head_sha is None:
        # Could not resolve refs — nothing more to show.
        return

    console.print(
        f"[dim]Base: {result.base_sha[:8] if result.base_sha else '?'} → "
        f"Head: {result.head_sha[:8] if result.head_sha else '?'}[/dim]"
    )
    console.print()

    # --- Renamed tables ---
    if result.renamed_tables:
        console.print("[bold yellow]Renamed producers — verify consumers updated:[/bold yellow]")
        console.print("  (These are EXCLUDED from the data-loss blast radius.)")
        console.print(
            "  (Classification is a heuristic: both column-set AND consumer Jaccard ≥ 0.6.)"
        )
        rtbl = Table("old name", "new name")
        for old, new in sorted(result.renamed_tables.items()):
            rtbl.add_row(old, new)
        console.print(rtbl)
        console.print()

    # --- Genuine lost producers ---
    if result.lost_producer_tables:
        console.print("[bold red]Lost producers (genuine data-loss risk):[/bold red]")
        ltbl = Table("table", "dropped by files")
        for t in result.lost_producer_tables:
            files = result.attribution.get(t, [])
            ltbl.add_row(t, ", ".join(files) if files else "(unknown)")
        console.print(ltbl)
        console.print()
    else:
        console.print("[green]No genuine lost producers detected.[/green]")
        console.print()
        return

    # --- Blast radius (PR 1 two views) ---
    blast = result.blast_radius

    # N1 noise-filter already applied by the engine; --raw re-includes noise.
    row_tables = blast.row_empty_tables
    value_cols = blast.value_empty_columns
    val_tables = blast.value_affected_tables
    prop_order = blast.propagation_order
    if raw:
        row_tables = list(row_tables) + blast.noise_excluded
        val_tables = list(val_tables) + blast.noise_excluded

    console.print("[bold green]View 2 — Value derivation (PRIMARY)[/bold green]")
    console.print(
        "  Downstream columns/tables whose VALUES are transitively derived from the lost source."
    )
    console.print()
    if val_tables:
        fully_set = set(blast.value_fully_empty_tables)
        partially_set = set(blast.value_partially_empty_tables)
        vtbl = Table("table", "value-empty / total columns", "full|partial")
        for t in val_tables:
            ve_count = sum(1 for c in value_cols if c.rsplit(".", 1)[0] == t)
            marker = "full" if t in fully_set else ("partial" if t in partially_set else "?")
            vtbl.add_row(t, str(ve_count), marker)
        console.print(vtbl)
        n_vc = len(value_cols)
        n_vt = len(val_tables)
        console.print(f"  {n_vc} value-empty column(s) across {n_vt} table(s).")
    else:
        console.print("  [yellow]No value-derived downstream tables found.[/yellow]")
    console.print()

    console.print("[bold yellow]View 1 — Row reachability (SUPPLEMENT)[/bold yellow]")
    console.print(f"  {_ROW_EMPTY_LABEL}")
    console.print()
    if row_tables:
        rtbl2 = Table("table")
        for t in row_tables:
            rtbl2.add_row(t)
        console.print(rtbl2)
    else:
        console.print("  [yellow]No row-reachable downstream tables found.[/yellow]")
    console.print()

    if prop_order:
        console.print("[bold]Propagation order (closest-to-source first):[/bold]")
        for i, t in enumerate(prop_order, 1):
            console.print(f"  {i}. {t}")
        console.print()

    if blast.hint:
        console.print(f"[yellow]Hint:[/yellow] {blast.hint}")
    if blast.truncated:
        console.print(
            "[yellow]Warning:[/yellow] Traversal hit the 50k-node safety cap; "
            "results may be incomplete."
        )
    if blast.noise_excluded and not raw:
        console.print(
            f"[dim]{len(blast.noise_excluded)} table(s) excluded as backup/noise/synthetic "
            "(use --raw to include them).[/dim]"
        )
