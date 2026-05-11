#!/usr/bin/env python3
"""Diagnostic script to collect and categorize parsing errors from a SQL corpus.

This script does NOT write to KuzuDB. It runs pure parser analysis on a corpus
directory and produces a JSON report of error frequencies and timing metrics.

Usage:
    uv run python scripts/collect_parse_errors.py <corpus_dir> --dialect snowflake
        [--output results.json] [--max-files N] [--slow-threshold 1.0]
"""

import argparse
import json
import logging
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlglot
import sqlglot.expressions as exp
from sqlglot.lineage import lineage as sg_lineage

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.snowflake_parser import SnowflakeParser


class CommandFallbackCapture(logging.Handler):
    """Capture sqlglot 'Command' fallback warnings."""

    def __init__(self):
        super().__init__()
        self.warnings: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        if "Command" in msg or "unsupported syntax" in msg:
            self.warnings.append(msg)


def _load_schema_dict(csv_path: Path) -> dict:
    """Build a sqlglot-compatible schema dict from an INFORMATION_SCHEMA CSV.

    Returns {table_schema: {table_name: [col_name, ...]}} keyed by lowercased parts.
    """
    import csv

    schema: dict = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            db = row["TABLE_SCHEMA"].lower()
            table = row["TABLE_NAME"].lower()
            col = row["COLUMN_NAME"].lower()
            schema.setdefault(db, {}).setdefault(table, []).append(col)
    return schema


def collect_parse_errors(
    corpus_dir: str,
    dialect: str = "snowflake",
    output: str = "parse_errors_results.json",
    max_files: int | None = None,
    slow_threshold: float = 1.0,
    schema_csv: str | None = None,
) -> None:
    """Scan a SQL corpus and collect error statistics.

    Args:
        corpus_dir: Path to directory tree of .sql files (recursive)
        dialect: sqlglot dialect string (default: snowflake)
        output: Path to write JSON results
        max_files: Max number of files to process (None = all)
        slow_threshold: Files taking longer than this many seconds are flagged
        schema_csv: Optional path to INFORMATION_SCHEMA CSV; if provided, schema is
            passed to sg_lineage() to improve column qualification
    """
    corpus_path = Path(corpus_dir)
    if not corpus_path.is_dir():
        print(f"Error: {corpus_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    schema: dict = {}
    if schema_csv:
        csv_path = Path(schema_csv)
        if not csv_path.is_file():
            print(f"Error: schema CSV not found: {schema_csv}", file=sys.stderr)
            sys.exit(1)
        print(f"Loading schema from {schema_csv}...")
        schema = _load_schema_dict(csv_path)
        print(f"  Loaded {sum(len(v) for v in schema.values())} tables from {len(schema)} schemas.")

    # Discover all .sql files
    print(f"Scanning {corpus_dir} for .sql files...")
    sql_files = sorted(corpus_path.rglob("*.sql"))
    if max_files:
        sql_files = sql_files[:max_files]

    print(f"Found {len(sql_files)} files.")
    if max_files:
        print(f"Processing first {max_files} files.")
    print()

    # Set up sqlglot logger capture
    handler = CommandFallbackCapture()
    logging.getLogger("sqlglot").addHandler(handler)

    # Initialize parser
    resolver = SchemaResolver(dialect=dialect if dialect != "ansi" else None)
    if dialect == "snowflake":
        parser = SnowflakeParser(resolver)
    else:
        parser = AnsiParser(resolver)

    # Error categories
    errors = {
        "E1": {"count": 0, "example_files": [], "example_messages": []},
        "E2": {"count": 0, "example_files": [], "example_messages": []},
        "E3": {"count": 0, "example_files": [], "example_messages": []},
        "E4": {"count": 0, "example_files": [], "example_messages": []},
        "E5": {"count": 0, "example_files": [], "example_messages": []},
        "E6": {"count": 0, "example_files": [], "example_messages": []},
        "E7": {"count": 0, "example_files": [], "example_messages": []},
        "E8": {"count": 0, "example_files": [], "example_messages": []},
    }

    # Timing and success tracking
    per_file_times: list[float] = []
    slow_files: list[dict[str, Any]] = []
    success_edges = 0
    files_with_success = 0
    total_statements = 0
    total_cols_processed = 0
    total_e2_skipped = 0

    # Per-file timing bucket
    files_ok = 0
    files_degraded = 0
    files_failed = 0

    t_start = time.perf_counter()

    # Try to import tqdm for progress; fall back to simple progress
    try:
        from tqdm import tqdm

        pbar = tqdm(sql_files, unit="file", desc="Processing")
    except ImportError:
        pbar = None

    for file_idx, file_path in enumerate(sql_files):
        # Manual progress every 100 files if tqdm unavailable
        if pbar is None and (file_idx + 1) % 100 == 0:
            print(f"  Processed {file_idx + 1}/{len(sql_files)} files...")

        t0 = time.perf_counter()

        try:
            sql_text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            errors["E4"]["count"] += 1
            if len(errors["E4"]["example_files"]) < 10:
                errors["E4"]["example_files"].append(str(file_path.relative_to(corpus_path)))
            if len(errors["E4"]["example_messages"]) < 5:
                errors["E4"]["example_messages"].append(f"read error: {exc}")
            files_failed += 1
            if pbar:
                pbar.update(1)
            continue

        # Parse the file
        try:
            handler.warnings.clear()
            parsed = parser.parse_file(file_path, sql_text)
        except Exception as exc:
            errors["E4"]["count"] += 1
            if len(errors["E4"]["example_files"]) < 10:
                errors["E4"]["example_files"].append(str(file_path.relative_to(corpus_path)))
            if len(errors["E4"]["example_messages"]) < 5:
                errors["E4"]["example_messages"].append(str(exc))
            files_failed += 1
            if pbar:
                pbar.update(1)
            continue

        # Check for parse failures (E4)
        if parsed.parse_quality.value == "FAILED":
            errors["E4"]["count"] += 1
            if len(errors["E4"]["example_files"]) < 10:
                errors["E4"]["example_files"].append(str(file_path.relative_to(corpus_path)))
            # Extract first error message if available
            if parsed.errors and len(errors["E4"]["example_messages"]) < 5:
                first_error = parsed.errors[0]
                # Strip prefix if present
                if ":" in first_error:
                    first_error = first_error.split(":", 1)[1]
                if len(first_error) > 100:
                    first_error = first_error[:100] + "..."
                errors["E4"]["example_messages"].append(first_error)

        # Check for E3 (Command fallback)
        if handler.warnings:
            for warning in handler.warnings:
                errors["E3"]["count"] += 1
                if len(errors["E3"]["example_files"]) < 10:
                    errors["E3"]["example_files"].append(str(file_path.relative_to(corpus_path)))
                if len(errors["E3"]["example_messages"]) < 5:
                    errors["E3"]["example_messages"].append(warning)

        total_statements += len(parsed.statements)

        # Classify file by parse quality
        if parsed.parse_quality.value == "FAILED":
            files_failed += 1
        elif parsed.parse_quality.value == "FULL":
            files_ok += 1
        else:
            files_degraded += 1

        # Extract errors that the parser already recorded
        for error_msg in parsed.errors:
            if "tree_walk" in error_msg:
                # E7: tree_walk_fail
                errors["E7"]["count"] += 1
                if len(errors["E7"]["example_files"]) < 10:
                    errors["E7"]["example_files"].append(str(file_path.relative_to(corpus_path)))
                if len(errors["E7"]["example_messages"]) < 5:
                    msg = error_msg.split(":")[-1] if ":" in error_msg else error_msg
                    if len(msg) > 100:
                        msg = msg[:100] + "..."
                    errors["E7"]["example_messages"].append(msg)
            elif "col_lineage:" in error_msg and "tree_walk" not in error_msg:
                # E5: lineage_other (from col_lineage errors that aren't tree_walk)
                # Check for E1 vs E5
                if "Cannot find column" in error_msg and "." in error_msg:
                    # Likely E1 but hard to disambiguate from error message alone
                    errors["E5"]["count"] += 1
                    if len(errors["E5"]["example_files"]) < 10:
                        errors["E5"]["example_files"].append(
                            str(file_path.relative_to(corpus_path))
                        )
                    if len(errors["E5"]["example_messages"]) < 5:
                        errors["E5"]["example_messages"].append(error_msg)
                else:
                    errors["E5"]["count"] += 1
                    if len(errors["E5"]["example_files"]) < 10:
                        errors["E5"]["example_files"].append(
                            str(file_path.relative_to(corpus_path))
                        )
                    if len(errors["E5"]["example_messages"]) < 5:
                        errors["E5"]["example_messages"].append(error_msg)

        # Extract column lineage from each statement
        file_had_success = False
        for stmt in parsed.statements:
            if not stmt.column_lineage:
                # Try to extract column lineage inline
                try:
                    # Rerun _extract_column_lineage to count errors and E8
                    stmt_obj = None
                    # We need to re-parse to get the AST, use the sql text
                    try:
                        stmts = sqlglot.parse(stmt.sql, dialect=dialect)
                        if stmts and stmts[0]:
                            stmt_obj = stmts[0]
                    except Exception:
                        pass

                    if stmt_obj:
                        # Extract column expressions
                        col_expressions = []
                        if isinstance(stmt_obj, exp.Select):
                            col_expressions = stmt_obj.expressions
                        elif isinstance(stmt_obj, exp.Create) and isinstance(
                            stmt_obj.expression, exp.Select
                        ):
                            col_expressions = stmt_obj.expression.expressions
                        elif isinstance(stmt_obj, exp.Insert) and isinstance(
                            stmt_obj.expression, exp.Select
                        ):
                            col_expressions = stmt_obj.expression.expressions

                        for col_expr in col_expressions:
                            total_cols_processed += 1

                            # Determine col_name
                            if col_expr.alias:
                                col_name = col_expr.alias
                            elif isinstance(col_expr, exp.Column):
                                col_name = col_expr.name
                            else:
                                # E2: expression with no resolvable name
                                errors["E2"]["count"] += 1
                                total_e2_skipped += 1
                                if len(errors["E2"]["example_files"]) < 10:
                                    errors["E2"]["example_files"].append(
                                        str(file_path.relative_to(corpus_path))
                                    )
                                if len(errors["E2"]["example_messages"]) < 5:
                                    ex_str = col_expr.sql(dialect=dialect)
                                    if len(ex_str) > 100:
                                        ex_str = ex_str[:100] + "..."
                                    errors["E2"]["example_messages"].append(ex_str)
                                continue

                            # Try sg_lineage
                            try:
                                if isinstance(stmt_obj, exp.Select):
                                    body = stmt_obj
                                elif isinstance(stmt_obj, exp.Create) and isinstance(
                                    stmt_obj.expression, exp.Select
                                ):
                                    body = stmt_obj.expression
                                elif isinstance(stmt_obj, exp.Insert) and isinstance(
                                    stmt_obj.expression, exp.Select
                                ):
                                    body = stmt_obj.expression
                                else:
                                    continue

                                root = sg_lineage(col_name, body, schema=schema, dialect=dialect)

                                if root:
                                    # Check if edges were produced
                                    new_edges = parser._lineage_node_to_edges(
                                        root,
                                        dst_col_name=col_name,
                                        dst_table=None,
                                        path=file_path,
                                        out=parsed,
                                    )
                                    if new_edges:
                                        success_edges += len(new_edges)
                                        file_had_success = True
                                    else:
                                        # E8: root but no edges
                                        errors["E8"]["count"] += 1
                                        if len(errors["E8"]["example_files"]) < 10:
                                            errors["E8"]["example_files"].append(
                                                str(file_path.relative_to(corpus_path))
                                            )
                                        if len(errors["E8"]["example_messages"]) < 5:
                                            msg = f"col={col_name} root has no leaf sources"
                                            errors["E8"]["example_messages"].append(msg)

                            except Exception as exc:
                                exc_str = str(exc)

                                # Disambiguate E1 vs E5
                                if "Cannot find column" in exc_str and "." in col_name:
                                    errors["E1"]["count"] += 1
                                    if len(errors["E1"]["example_files"]) < 10:
                                        errors["E1"]["example_files"].append(
                                            str(file_path.relative_to(corpus_path))
                                        )
                                    if len(errors["E1"]["example_messages"]) < 5:
                                        errors["E1"]["example_messages"].append(col_name)
                                else:
                                    # E5: lineage_other
                                    errors["E5"]["count"] += 1
                                    if len(errors["E5"]["example_files"]) < 10:
                                        errors["E5"]["example_files"].append(
                                            str(file_path.relative_to(corpus_path))
                                        )
                                    if len(errors["E5"]["example_messages"]) < 5:
                                        if len(exc_str) > 100:
                                            exc_str = exc_str[:100] + "..."
                                        errors["E5"]["example_messages"].append(exc_str)

                except Exception:
                    pass  # Skip file if something goes wrong

        if file_had_success:
            files_with_success += 1

        t1 = time.perf_counter()
        elapsed = t1 - t0
        per_file_times.append(elapsed)

        if elapsed > slow_threshold:
            slow_files.append(
                {
                    "file": str(file_path.relative_to(corpus_path)),
                    "elapsed_seconds": round(elapsed, 2),
                    "statement_count": len(parsed.statements),
                }
            )

        if pbar:
            pbar.update(1)

    if pbar:
        pbar.close()

    t_end = time.perf_counter()
    total_time = t_end - t_start

    # Compute percentiles
    if per_file_times:
        p50 = statistics.median(per_file_times)
        # quantiles requires at least 2 points
        if len(per_file_times) >= 3:
            quantiles = statistics.quantiles(per_file_times, n=100)
            p95 = quantiles[94] if len(quantiles) > 94 else per_file_times[-1]
            p99 = quantiles[98] if len(quantiles) > 98 else per_file_times[-1]
        else:
            p95 = max(per_file_times)
            p99 = max(per_file_times)
    else:
        p50 = p95 = p99 = 0

    # Build result JSON
    result = {
        "run_date": datetime.now().isoformat(),
        "corpus_dir": str(corpus_path),
        "dialect": dialect,
        "total_files": len(sql_files),
        "total_statements": total_statements,
        "total_columns_processed": total_cols_processed,
        "runtime_seconds": round(total_time, 1),
        "error_summary": errors,
        "success_edges": success_edges,
        "files_with_success": files_with_success,
        "files_ok": files_ok,
        "files_degraded": files_degraded,
        "files_failed": files_failed,
        "slow_files": slow_files[:20],  # Top 20 slowest files
        "per_file_timing_p50_ms": round(p50 * 1000, 1),
        "per_file_timing_p95_ms": round(p95 * 1000, 1),
        "per_file_timing_p99_ms": round(p99 * 1000, 1),
    }

    # Write JSON
    output_path = Path(output)
    output_path.write_text(json.dumps(result, indent=2))
    print(f"\nResults written to: {output_path}")

    # Print summary
    print("\n=== Error Frequency Table ===")
    total_col_exprs = total_cols_processed
    if total_col_exprs == 0:
        total_col_exprs = 1  # Avoid division by zero
    n1, n2, n3, n4, n5 = (errors[k]["count"] for k in ("E1", "E2", "E3", "E4", "E5"))
    e1_pct = 100 * n1 / total_col_exprs
    e2_pct = 100 * n2 / total_col_exprs
    e5_pct = 100 * n5 / total_col_exprs
    e3_files = len(set(errors["E3"]["example_files"]))
    e4_files = len(set(errors["E4"]["example_files"]))
    print(f"E1  alias_col_ref      {n1:6d} occurrences ({e1_pct:5.1f}% of col exprs)")
    print(f"E2  expr_no_name       {n2:6d} occurrences ({e2_pct:5.1f}% of col exprs)  <- skipped")
    print(f"E3  command_fallback   {n3:6d} occurrences (Command fallback in {e3_files:3d} files)")
    print(f"E4  parse_failure      {n4:6d} occurrences ({e4_files:3d} files, full skip)")
    print(f"E5  lineage_other      {n5:6d} occurrences ({e5_pct:5.1f}% of col exprs)")
    e6_note = f"from {len(schema)} schemas" if schema else "schema was empty in this run"
    print(f"E6  schema_mismatch    {errors['E6']['count']:6d} occurrences ({e6_note})")
    print(f"E7  tree_walk_fail     {errors['E7']['count']:6d} occurrences")
    print(f"E8  no_edges_from_root {errors['E8']['count']:6d} occurrences")

    print("\n=== Success ===")
    total_files = len(sql_files)
    pct_resolved = 100 * files_with_success / total_files if total_files else 0
    print(
        f"sg_lineage edges emitted:  {success_edges:,} "
        f"(from {files_with_success} of {total_files} files — {pct_resolved:.1f}% resolved)"
    )

    print("\n=== File Quality ===")
    print(f"Files parsed OK (FULL):    {files_ok}")
    print(f"Files degraded:            {files_degraded}")
    print(f"Files failed:              {files_failed}")

    if slow_files:
        print(f"\n=== Slow Files (> {slow_threshold}s) ===")
        for item in slow_files[:10]:
            stmts = item["statement_count"]
            print(f"{item['elapsed_seconds']:5.1f}s  {item['file']:50s}  ({stmts} statements)")

    print("\n=== Per-File Timing ===")
    print(f"p50: {result['per_file_timing_p50_ms']:.1f} ms")
    print(f"p95: {result['per_file_timing_p95_ms']:.1f} ms")
    print(f"p99: {result['per_file_timing_p99_ms']:.1f} ms")
    print(f"\nTotal runtime: {total_time:.1f}s")


def main():
    """Parse CLI arguments and run the experiment."""
    parser = argparse.ArgumentParser(
        description="Collect parsing errors from a SQL corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python scripts/collect_parse_errors.py /home/ignwrad/Projects/dwh --dialect snowflake
  uv run python scripts/collect_parse_errors.py /home/ignwrad/Projects/dwh \\
      --max-files 200 --output results_quick.json
        """,
    )

    parser.add_argument("corpus_dir", help="Path to directory tree of .sql files (recursive)")
    parser.add_argument(
        "--dialect",
        default="snowflake",
        help="sqlglot dialect string (default: snowflake)",
    )
    parser.add_argument(
        "--output",
        default="parse_errors_results.json",
        help="Path to write JSON results (default: parse_errors_results.json)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Max number of files to process (None = all)",
    )
    parser.add_argument(
        "--slow-threshold",
        type=float,
        default=1.0,
        help="Files taking longer than this many seconds are flagged (default: 1.0)",
    )
    parser.add_argument(
        "--schema-csv",
        default=None,
        help="Path to INFORMATION_SCHEMA.COLUMNS CSV to pass schema to sg_lineage()",
    )

    args = parser.parse_args()

    collect_parse_errors(
        corpus_dir=args.corpus_dir,
        dialect=args.dialect,
        output=args.output,
        max_files=args.max_files,
        slow_threshold=args.slow_threshold,
        schema_csv=args.schema_csv,
    )


if __name__ == "__main__":
    main()
