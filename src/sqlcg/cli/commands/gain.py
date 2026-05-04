"""The sqlcg gain command — view metrics and feedback analytics."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from sqlcg.metrics.store import MetricsStore
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

    All metrics are opt-in via SQLCG_METRICS environment variable.
    If no metrics have been collected, shows a message and exits 0.
    """
    if _metrics_path is None:
        metrics_path = Path.home() / ".sqlcg" / "metrics.db"
    else:
        metrics_path = _metrics_path

    if not metrics_path.exists():
        if json_output:
            console.print(
                json.dumps(
                    {
                        "total_calls": 0,
                        "last_7d_calls": 0,
                        "index_runs": 0,
                        "feedback_tp": 0,
                        "feedback_total": 0,
                        "top_tools": [],
                    }
                )
            )
        else:
            console.print("No metrics collected yet.")
        return

    try:
        metrics = MetricsStore(metrics_path)
        metrics.init_schema()  # Ensure schema exists

        # Section A: Total calls and last 7 days
        all_calls = metrics.execute_query("SELECT COUNT(*) as count FROM tool_calls")
        total_calls = all_calls[0][0] if all_calls else 0

        cutoff_date = (datetime.utcnow() - timedelta(days=7)).isoformat()
        last_7d = metrics.execute_query(
            f"SELECT COUNT(*) as count FROM tool_calls WHERE timestamp > '{cutoff_date}'"
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

        if json_output:
            console.print(
                json.dumps(
                    {
                        "total_calls": total_calls,
                        "last_7d_calls": last_7d_calls,
                        "index_runs": len(index_runs),
                        "feedback_tp": tp_count,
                        "feedback_total": fb_total,
                        "top_tools": [{"name": row[0], "count": row[1]} for row in top_tools],
                    }
                )
            )
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

        metrics.close()

    except Exception as exc:
        logger.error(f"Failed to generate metrics report: {exc}")
        if json_output:
            console.print(json.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Error: {exc}[/red]")
