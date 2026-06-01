"""Analyze command for lineage analysis."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from sqlcg.core.config import get_backend
from sqlcg.core.queries import GET_TABLE_EXTERNAL_CONSUMERS_QUERY
from sqlcg.core.schema import NodeLabel, RelType

if TYPE_CHECKING:
    from sqlcg.server.noise_filter import NoiseFilter

app = typer.Typer(help="Lineage analysis")
console = Console()


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
    # Bounds check for depth to prevent performance DoS
    if depth < 1 or depth > 100:
        console.print("[red]Error: --depth must be between 1 and 100[/red]")
        raise typer.Exit(1)

    # By default, filter out CTE/derived intermediate nodes; --include-intermediate restores them.
    # Half B (#38): use OPTIONAL MATCH so a missing SqlTable node (e.g. CTE-body source not yet
    # re-indexed after #39 fix) is KEPT rather than silently dropped.  WHERE t.kind IS NULL OR
    # t.kind IN [...] means: keep when node absent (NULL) OR when kind is a physical source.
    # CTE aliases (kind='cte') and derived tables (kind='derived') are filtered out.
    kind_filter = (
        ""
        if include_intermediate
        else "OPTIONAL MATCH (t:SqlTable {qualified: src.table_qualified}) "
        "WITH c, src, t WHERE t.kind IS NULL OR t.kind IN ['table', 'external'] "
    )

    with get_backend(read_only=True) as backend:
        results = backend.run_read(
            f"MATCH (c:{NodeLabel.COLUMN} {{id: $ref}})"
            f"<-[:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src:{NodeLabel.COLUMN}) "
            f"{kind_filter}"
            f"OPTIONAL MATCH (src)-[direct:{RelType.COLUMN_LINEAGE}]->(c) "
            "OPTIONAL MATCH (q:SqlQuery {id: direct.query_id}) "
            "RETURN src.id AS id, q.file_path AS file, q.start_line AS line LIMIT 100",
            {"ref": ref},
        )
        if not results and len(ref.split(".")) >= 3:
            bare = _bare_ref(ref)
            fallback_results = backend.run_read(
                f"MATCH (c:{NodeLabel.COLUMN} {{id: $bare}})"
                f"<-[:{RelType.COLUMN_LINEAGE}*1..{depth}]-(src:{NodeLabel.COLUMN}) "
                f"{kind_filter}"
                f"OPTIONAL MATCH (src)-[direct:{RelType.COLUMN_LINEAGE}]->(c) "
                "OPTIONAL MATCH (q:SqlQuery {id: direct.query_id}) "
                "RETURN src.id AS id, q.file_path AS file, q.start_line AS line LIMIT 100",
                {"bare": bare},
            )
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

            nf = NoiseFilter.from_config()  # repo_root=None → falls back to Path.cwd()
            results = _filter_column_results(results, nf)
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
    # Bounds check for depth to prevent performance DoS
    if depth < 1 or depth > 100:
        console.print("[red]Error: --depth must be between 1 and 100[/red]")
        raise typer.Exit(1)

    # By default, filter out CTE/derived intermediate nodes; --include-intermediate restores them.
    # Half B (#38): OPTIONAL MATCH keeps sources whose SqlTable node is absent (NULL) or is a
    # physical kind.  WITH c, dst, t carries the three variables in scope at this interpolation
    # point; direct and q are bound later in the query.
    kind_filter = (
        ""
        if include_intermediate
        else "OPTIONAL MATCH (t:SqlTable {qualified: dst.table_qualified}) "
        "WITH c, dst, t WHERE t.kind IS NULL OR t.kind IN ['table', 'external'] "
    )

    with get_backend(read_only=True) as backend:
        results = backend.run_read(
            f"MATCH (c:{NodeLabel.COLUMN} {{id: $ref}})"
            f"-[:{RelType.COLUMN_LINEAGE}*1..{depth}]->(dst:{NodeLabel.COLUMN}) "
            f"{kind_filter}"
            f"OPTIONAL MATCH (c)-[direct:{RelType.COLUMN_LINEAGE}]->(dst) "
            "OPTIONAL MATCH (q:SqlQuery {id: direct.query_id}) "
            "RETURN dst.id AS id, q.file_path AS file, q.start_line AS line LIMIT 100",
            {"ref": ref},
        )
        if not results and len(ref.split(".")) >= 3:
            bare = _bare_ref(ref)
            fallback_results = backend.run_read(
                f"MATCH (c:{NodeLabel.COLUMN} {{id: $bare}})"
                f"-[:{RelType.COLUMN_LINEAGE}*1..{depth}]->(dst:{NodeLabel.COLUMN}) "
                f"{kind_filter}"
                f"OPTIONAL MATCH (c)-[direct:{RelType.COLUMN_LINEAGE}]->(dst) "
                "OPTIONAL MATCH (q:SqlQuery {id: direct.query_id}) "
                "RETURN dst.id AS id, q.file_path AS file, q.start_line AS line LIMIT 100",
                {"bare": bare},
            )
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

            nf = NoiseFilter.from_config()  # repo_root=None → falls back to Path.cwd()
            results = _filter_column_results(results, nf)
        _print_table(_add_file_line_col(results), ["id", "file:line"])

        # Append external consumer rows for terminal tables (scalar query, one per terminal).
        # Resolve terminal tables from the column results; fall back to the root column's table.
        terminal_tables: set[str] = set()
        for r in results:
            tbl = _col_id_to_table(r["id"])
            if tbl:
                terminal_tables.add(tbl)
        # Also check the root column's table (in case no downstream columns were found).
        root_parts = ref.rsplit(".", 1)
        if len(root_parts) == 2:
            terminal_tables.add(root_parts[0])
        consumer_rows: list[dict] = []
        for tbl in sorted(terminal_tables):
            rows_ec = backend.run_read(
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
    with get_backend(read_only=True) as backend:
        results = backend.run_read(
            f"MATCH (t:{NodeLabel.TABLE} {{qualified: $t}})"
            f"<-[:{RelType.SELECTS_FROM}]-(q:{NodeLabel.QUERY}) "
            "RETURN DISTINCT q.id AS id, q.kind AS kind, q.target_table AS target LIMIT 100",
            {"t": table},
        )
        if not raw:
            from sqlcg.server.noise_filter import NoiseFilter

            nf = NoiseFilter.from_config()
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
    """List files that failed to parse, with their dominant cause (E-code bucket).

    Valid --cause buckets (from highest to lowest severity):
    timeout, E8, E3, E2, E5, E1, qualify_failed, func_fallback, pure_ddl_skip.

    Requires a graph indexed with sqlcg >= v3 (schema version 3). Re-index
    with 'sqlcg db reset && sqlcg index <path>' if the graph was built with
    an earlier version.
    """
    with get_backend(read_only=True) as backend:
        cypher = (
            f"MATCH (f:{NodeLabel.FILE}) WHERE f.parse_failed = true "
            "AND ($cause IS NULL OR f.parse_cause = $cause) "
            "RETURN f.path AS path, f.parse_cause AS cause "
            f"ORDER BY f.parse_cause LIMIT {limit}"
        )
        rows = backend.run_read(cypher, {"cause": cause})
        _print_table(rows, ["path", "cause"])


@app.command("unused")
def unused(
    threshold: int = typer.Option(0, "--threshold", help="Minimum reference count threshold"),
    raw: bool = typer.Option(False, "--raw", help="Disable noise filtering on results"),  # noqa: B008
) -> None:
    """Find tables with no query references."""
    with get_backend(read_only=True) as backend:
        results = backend.run_read(
            f"MATCH (t:{NodeLabel.TABLE}) WHERE NOT (t)<-[:{RelType.SELECTS_FROM}]-() "
            "RETURN DISTINCT t.qualified AS qualified LIMIT 100",
            {},
        )
        if not raw:
            from sqlcg.server.noise_filter import NoiseFilter

            nf = NoiseFilter.from_config()
            results = [r for r in results if not nf.is_noise(r["qualified"])]
        _print_table(results, ["qualified"])


def _bare_ref(ref: str) -> str:
    """Strip schema prefix from a ref string, keeping table.column.

    For a 3-part ref ("mart.fact_t.amount") this returns "fact_t.amount".
    For a 2-part ref ("fact_t.amount") this returns the ref unchanged.
    Never uses rsplit — that would yield only the column name for 3-part refs.
    """
    parts = ref.split(".")
    if len(parts) >= 3:
        return ".".join(parts[1:])  # drop schema, keep table.column
    return ref  # already bare (no schema prefix)


def _col_id_to_table(col_id: str) -> str:
    """Extract the table-qualified part from a column ID (schema.table.col → schema.table).

    Column IDs follow the format: schema.table.column or table.column.
    The table part is everything except the last component.

    Args:
        col_id: A column ID string from the graph.

    Returns:
        The table-qualified portion (all but the last dotted component).
    """
    parts = col_id.rsplit(".", 1)
    return parts[0] if len(parts) == 2 else col_id


def _filter_column_results(
    results: list[dict],
    nf: NoiseFilter,  # type: ignore[name-defined]
) -> list[dict]:
    """Filter column-ID result rows by NoiseFilter, dropping rows whose table is noise."""
    return [r for r in results if not nf.is_noise(_col_id_to_table(r["id"]))]


def _add_file_line_col(rows: list[dict]) -> list[dict]:
    """Add a 'file:line' composite column from 'file' and 'line' fields.

    Formats as 'path/to/file.sql:N' when both are present, or '?' when either
    is absent (multi-hop upstream where file/line is not available).
    """
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
