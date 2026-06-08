"""The sqlcg gain command — view metrics and feedback analytics."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from sqlcg.cli.coverage import (
    blindspot_colour,
    collect_coverage,
    edge_health_colour,
    phantom_colour,
)
from sqlcg.metrics import store as metrics_module
from sqlcg.server.read_client import run_read_routed
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)
console = Console()


def gain_cmd(
    json_output: bool = typer.Option(  # noqa: B008
        False,
        "--json",
        help="Output metrics as JSON",
    ),
    _metrics_path: Path | None = None,  # For testing only
) -> None:
    """Show metrics and feedback analytics.

    Displays:
    - Section A: Total MCP tool calls and calls in the last 7 days
    - Section B: Parse success trend (last 5 index runs)
    - Section C: True positive feedback rate (if ≥5 samples)
    - Section D: Top 3 most-called tools
    - Section E: execute_cypher ratio (high ratio = LLM falling back to raw Cypher)
    - Section F: Parse quality breakdown from graph (FULL / TABLE_ONLY / SCRIPTING_FALLBACK)

    Parse quality legend:
      FULL              — column-level lineage extracted; all tools work
      TABLE_ONLY        — table edges only; trace_column_lineage returns empty
      SCRIPTING_FALLBACK— sqlglot fell back to Command node; partial table edges only

    All metrics are opt-in via SQLCG_METRICS environment variable.
    If no metrics have been collected, shows a message and exits 0.
    """
    json_output = json_output is True

    if _metrics_path is None:
        metrics_path = Path.home() / ".sqlcg" / "metrics.db"
    else:
        metrics_path = _metrics_path

    # Coverage is graph-derived — available even before any metrics are collected.
    coverage = collect_coverage()

    if not metrics_path.exists():
        if json_output:
            payload: dict = {
                "total_calls": 0,
                "last_7d_calls": 0,
                "index_runs": 0,
                "feedback_tp": 0,
                "feedback_total": 0,
                "top_tools": [],
            }
            if coverage is not None:
                payload["coverage"] = {
                    "catalogued_tables": coverage.catalogued_tables,
                    "total_tables": coverage.total_tables,
                    "good_edges": coverage.good_edges,
                    "total_edges": coverage.total_edges,
                    "phantom_edges": coverage.phantom_edges,
                    "blindspot_tables": coverage.blindspot_tables,
                }
            console.print(json.dumps(payload))
        else:
            console.print("No metrics collected yet.")
            if coverage is not None:
                console.print()
                console.print("[bold cyan]G. Coverage[/bold cyan]")
                console.print(
                    f"  Tables with catalog: {coverage.catalogued_tables} / {coverage.total_tables}"
                    f" ({coverage.catalog_pct:.0f}%)"
                )
                eh_colour = edge_health_colour(coverage.edge_health_pct)
                console.print(
                    f"  [{eh_colour}]Edge health: {coverage.good_edges} / {coverage.total_edges}"
                    f" ({coverage.edge_health_pct:.0f}%)[/{eh_colour}]"
                )
                ph_colour = phantom_colour(coverage.phantom_pct)
                console.print(
                    f"  [{ph_colour}]Phantom edges: {coverage.phantom_edges}"
                    f" / {coverage.total_edges} ({coverage.phantom_pct:.0f}%)"
                    f"[/{ph_colour}]"
                )
                bs_colour = blindspot_colour(coverage.blindspot_tables)
                if bs_colour:
                    console.print(
                        f"  [{bs_colour}]Blindspot tables:"
                        f" {coverage.blindspot_tables}[/{bs_colour}]"
                    )
                else:
                    console.print(f"  Blindspot tables: {coverage.blindspot_tables}")
        return

    try:
        metrics = metrics_module.MetricsStore(metrics_path)
        metrics.init_schema()  # Ensure schema exists

        # Section A: Total calls and last 7 days
        all_calls = metrics.execute_query("SELECT COUNT(*) as count FROM tool_calls")
        total_calls = all_calls[0][0] if all_calls else 0

        cutoff_date = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        last_7d = metrics.execute_query(
            "SELECT COUNT(*) as count FROM tool_calls WHERE timestamp > ?",
            (cutoff_date,),
        )
        last_7d_calls = last_7d[0][0] if last_7d else 0

        # Section B: Parse success (last 5 index runs)
        index_runs = metrics.execute_query(
            """
            SELECT files_parsed, parse_errors
            FROM index_runs
            ORDER BY timestamp DESC
            LIMIT 5
            """
        )

        # Section C: TP feedback rate
        feedback_counts = metrics.execute_query(
            """
            SELECT
                SUM(CASE WHEN label = 'TP' THEN 1 ELSE 0 END) as tp_count,
                COUNT(*) as total
            FROM feedback
            """
        )
        tp_count = feedback_counts[0][0] or 0 if feedback_counts else 0
        fb_total = feedback_counts[0][1] or 0 if feedback_counts else 0

        # Section D: Top 3 tools
        top_tools = metrics.execute_query(
            """
            SELECT tool_name, COUNT(*) as count
            FROM tool_calls
            GROUP BY tool_name
            ORDER BY count DESC
            LIMIT 3
            """
        )

        # Section E: execute_sql ratio
        sql_query = "SELECT COUNT(*) as count FROM tool_calls WHERE tool_name = 'execute_sql'"
        execute_sql_count_result = metrics.execute_query(sql_query)
        execute_sql_count = execute_sql_count_result[0][0] if execute_sql_count_result else 0
        execute_sql_ratio = execute_sql_count / total_calls if total_calls > 0 else 0

        # Section F: parse quality from graph.
        # run_read_routed raises typer.Exit (Exception-derived, NOT SystemExit) on
        # server-busy timeout, so the except-Exception block degrades gracefully
        # (skips the parse-quality section) instead of crashing gain (WARNING 3).
        parse_quality: dict[str, int] | None = None
        try:
            mode_rows = run_read_routed(
                'SELECT parsing_mode AS mode, count(*) AS cnt FROM "SqlQuery"'
                " GROUP BY parsing_mode ORDER BY cnt DESC",
                {},
            )
            if mode_rows and "mode" in mode_rows[0]:
                parse_quality = {str(r["mode"]): int(r["cnt"]) for r in mode_rows}
        except Exception:
            pass  # graph not available or server busy — skip quality section

        if json_output:
            payload: dict = {
                "total_calls": total_calls,
                "last_7d_calls": last_7d_calls,
                "index_runs": len(index_runs),
                "feedback_tp": tp_count,
                "feedback_total": fb_total,
                "top_tools": [{"name": row[0], "count": row[1]} for row in top_tools],
                "execute_sql_ratio": round(execute_sql_ratio, 2),
            }
            if parse_quality is not None:
                payload["parse_quality"] = parse_quality
            if coverage is not None:
                payload["coverage"] = {
                    "catalogued_tables": coverage.catalogued_tables,
                    "total_tables": coverage.total_tables,
                    "good_edges": coverage.good_edges,
                    "total_edges": coverage.total_edges,
                    "phantom_edges": coverage.phantom_edges,
                    "blindspot_tables": coverage.blindspot_tables,
                }
            console.print(json.dumps(payload))
        else:
            # Human-readable output
            console.print("\n[bold]SQL Code Graph Metrics[/bold]")
            console.print()

            # Section A
            console.print("[bold cyan]A. Tool Calls[/bold cyan]")
            console.print(f"  Total: {total_calls}")
            console.print(f"  Last 7 days: {last_7d_calls}")
            console.print()

            # Section B
            console.print("[bold cyan]B. Index Runs[/bold cyan]")
            if index_runs:
                console.print(f"  Recent runs: {len(index_runs)}")
                total_files = sum(row[0] for row in index_runs)
                total_errors = sum(row[1] for row in index_runs)
                success_rate = (
                    100 * (total_files - total_errors) / total_files if total_files > 0 else 0
                )
                console.print(f"  Success rate (last 5): {success_rate:.1f}%")
            else:
                console.print("  No index runs recorded")
            console.print()

            # Section C
            if fb_total >= 5:
                tp_rate = 100 * tp_count / fb_total if fb_total > 0 else 0
                console.print("[bold cyan]C. Feedback[/bold cyan]")
                console.print(f"  True positive rate: {tp_rate:.1f}% ({tp_count}/{fb_total})")
            else:
                console.print("[bold cyan]C. Feedback[/bold cyan]")
                console.print(f"  Samples: {fb_total}/5 (need 5 to show TP rate)")
            console.print()

            # Section D
            if top_tools:
                console.print("[bold cyan]D. Top Tools[/bold cyan]")
                for i, (name, count) in enumerate(top_tools, 1):
                    console.print(f"  {i}. {name}: {count}")
            console.print()

            # Section E: execute_sql ratio
            console.print("[bold cyan]E. Raw SQL Usage[/bold cyan]")
            ratio_pct = execute_sql_ratio * 100
            if execute_sql_ratio > 0.3:
                msg = f"  [yellow]execute_sql: {ratio_pct:.1f}% (high raw-SQL usage)[/yellow]"
                console.print(msg)
            else:
                console.print(f"  execute_sql: {ratio_pct:.1f}%")
            console.print()

            # Section F: parse quality from graph
            if parse_quality:
                console.print("[bold cyan]F. Parse Quality[/bold cyan]")
                total_q = sum(parse_quality.values())
                for mode, cnt in sorted(parse_quality.items()):
                    pct = 100 * cnt / total_q if total_q else 0
                    label = {
                        "sqlglot": "standard (FULL/TABLE_ONLY)",
                        "scripting_block": "scripting fallback",
                    }.get(mode, mode)
                    console.print(f"  {label}: {cnt} ({pct:.0f}%)")
                scripting = parse_quality.get("scripting_block", 0)
                scripting_pct = 100 * scripting / total_q if total_q else 0
                if scripting_pct > 20:
                    console.print(
                        f"  [yellow]{scripting_pct:.0f}% scripting fallback — "
                        "column lineage limited for those files[/yellow]"
                    )
                console.print()

            # Section G: coverage — omitted silently when graph unavailable
            if coverage is not None:
                console.print("[bold cyan]G. Coverage[/bold cyan]")
                console.print(
                    f"  Tables with catalog: {coverage.catalogued_tables} / {coverage.total_tables}"
                    f" ({coverage.catalog_pct:.0f}%)"
                )
                eh_colour = edge_health_colour(coverage.edge_health_pct)
                console.print(
                    f"  [{eh_colour}]Edge health: {coverage.good_edges} / {coverage.total_edges}"
                    f" ({coverage.edge_health_pct:.0f}%)[/{eh_colour}]"
                )
                ph_colour = phantom_colour(coverage.phantom_pct)
                ph_line = (
                    f"  [{ph_colour}]Phantom edges: {coverage.phantom_edges}"
                    f" / {coverage.total_edges} ({coverage.phantom_pct:.0f}%)"
                    f"[/{ph_colour}]"
                )
                console.print(ph_line)
                bs_colour = blindspot_colour(coverage.blindspot_tables)
                if bs_colour:
                    console.print(
                        f"  [{bs_colour}]Blindspot tables:"
                        f" {coverage.blindspot_tables}[/{bs_colour}]"
                    )
                else:
                    console.print(f"  Blindspot tables: {coverage.blindspot_tables}")
                console.print()

        metrics.close()

    except Exception as exc:
        logger.error(f"Failed to generate metrics report: {exc}")
        if json_output:
            console.print(json.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Error: {exc}[/red]")
