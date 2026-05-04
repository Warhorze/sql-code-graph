"""The sqlcg report command — generate metrics and feedback reports."""

import hashlib
import urllib.parse
from pathlib import Path

import typer
from rich.console import Console

from sqlcg.metrics.store import MetricsStore
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)
console = Console()


def report_cmd(
    stdout: bool = typer.Option(False, "--stdout", help="Print to stdout instead of file"),  # noqa: B008
    output: Path | None = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Output file path",
    ),
) -> None:
    """Generate a metrics report with FP clusters and parse error patterns.

    Analyzes feedback and index run data to identify:
    - False positive clusters (files/patterns with >50% FP rate, min 3 samples)
    - Parse error clusters (repos with persistent parse errors)
    - Provides a pre-filled GitHub issue URL for reporting problems

    If no metrics database exists, prints a message and exits 0.
    """
    metrics_path = Path.home() / ".sqlcg" / "metrics.db"

    if not metrics_path.exists():
        message = "No metrics collected yet."
        if stdout:
            console.print(message)
        else:
            console.print(message)
        return

    try:
        metrics = MetricsStore(metrics_path)
        metrics.init_schema()

        # Section 1: FP clusters
        fp_clusters = metrics.execute_query(
            """
            SELECT query,
                   SUM(CASE WHEN label = 'FP' THEN 1 ELSE 0 END) AS fp_count,
                   COUNT(*) AS total,
                   CAST(SUM(CASE WHEN label = 'FP' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) AS fp_rate
            FROM feedback
            GROUP BY query
            HAVING total >= 3 AND fp_rate > 0.5
            ORDER BY fp_rate DESC
            """
        )

        # Section 2: Parse error clusters
        error_clusters = metrics.execute_query(
            """
            SELECT repo_path, COUNT(*) AS run_count,
                   SUM(parse_errors) AS total_errors,
                   CAST(SUM(parse_errors) AS REAL) / NULLIF(SUM(files_parsed), 0) AS error_rate
            FROM index_runs
            GROUP BY repo_path
            HAVING run_count >= 2 AND total_errors > 0
            ORDER BY total_errors DESC
            """
        )

        # Build report content
        report_lines = []

        # Section 1: FP clusters
        report_lines.append("## False Positive Clusters\n")
        if fp_clusters:
            report_lines.append("| Query | FP Count | Total | FP Rate |\n")
            report_lines.append("|---|---|---|---|\n")
            for row in fp_clusters:
                query, fp_count, total, fp_rate = row
                rate_pct = fp_rate * 100 if fp_rate else 0
                report_lines.append(f"| {query} | {fp_count} | {total} | {rate_pct:.1f}% |\n")
        else:
            report_lines.append("No FP clusters found (need ≥3 samples per query pattern).\n")
        report_lines.append("\n")

        # Section 2: Parse error clusters
        report_lines.append("## Parse Error Clusters\n")
        if error_clusters:
            report_lines.append("| Repo Path | Runs | Total Errors | Error Rate |\n")
            report_lines.append("|---|---|---|---|\n")
            for row in error_clusters:
                repo_path, run_count, total_errors, error_rate = row
                rate_pct = error_rate * 100 if error_rate else 0
                report_lines.append(
                    f"| {repo_path} | {run_count} | {total_errors} | {rate_pct:.1f}% |\n"
                )
        else:
            report_lines.append("No parse error clusters found.\n")
        report_lines.append("\n")

        report_content = "".join(report_lines)

        # Section 3: GitHub issue URL
        report_hash = hashlib.md5(report_content.encode()).hexdigest()[:8]
        issue_title = f"[sqlcg metrics] FP clusters report {report_hash}"
        issue_body_truncated = report_content[:2000]
        encoded_title = urllib.parse.quote(issue_title)
        encoded_body = urllib.parse.quote(issue_body_truncated)
        github_url = (
            f"https://github.com/Warhorze/sql-code-graph/issues/new?"
            f"title={encoded_title}&body={encoded_body}"
        )

        report_lines.append("## File an Issue\n")
        report_lines.append(f"[Report metrics issues on GitHub]({github_url})\n")

        final_report = "".join(report_lines)

        # Output
        if stdout:
            console.print(final_report)
        else:
            output_path = output or Path("docs/METRICS_REPORT.md")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(final_report)
            console.print(f"Report written to {output_path}")

        metrics.close()

    except Exception as exc:
        logger.error(f"Failed to generate report: {exc}")
        console.print(f"[red]Error: {exc}[/red]")
