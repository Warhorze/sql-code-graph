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


def collect_parse_errors(
    corpus_dir: str,
    dialect: str = "snowflake",
    output: str = "parse_errors_results.json",
    max_files: int | None = None,
    slow_threshold: float = 1.0,
    schema_csv: str | None = None,
) -> None:
    """Scan a SQL corpus and collect error statistics from production parser output.

    This script measures errors that the production parser (AnsiParser/SnowflakeParser)
    has already recorded in parsed.errors, along with edge counts from parsed.statements.
    It does NOT run its own sg_lineage; instead it classifies the parser's error messages.

    Args:
        corpus_dir: Path to directory tree of .sql files (recursive)
        dialect: sqlglot dialect string (default: snowflake)
        output: Path to write JSON results
        max_files: Max number of files to process (None = all)
        slow_threshold: Files taking longer than this many seconds are flagged
        schema_csv: Optional path to INFORMATION_SCHEMA CSV to load into SchemaResolver
            so the parser sees the same schema the experiment is measuring
    """
    corpus_path = Path(corpus_dir)
    if not corpus_path.is_dir():
        print(f"Error: {corpus_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Discover all .sql files
    print(f"Scanning {corpus_dir} for .sql files...")
    sql_files = sorted(corpus_path.rglob("*.sql"))
    if max_files:
        sql_files = sql_files[:max_files]

    print(f"Found {len(sql_files)} files.")
    if max_files:
        print(f"Processing first {max_files} files.")
    print()

    # Set up sqlglot logger capture for E3 (Command fallback warnings)
    handler = CommandFallbackCapture()
    logging.getLogger("sqlglot").addHandler(handler)

    # Initialize parser with SchemaResolver
    resolver = SchemaResolver(dialect=dialect if dialect != "ansi" else None)

    # Load INFORMATION_SCHEMA into SchemaResolver so parser has access to schema
    if schema_csv:
        csv_path = Path(schema_csv)
        if not csv_path.is_file():
            print(f"Error: schema CSV not found: {schema_csv}", file=sys.stderr)
            sys.exit(1)
        print(f"Loading schema from {schema_csv}...")
        try:
            num_tables = resolver.add_information_schema(csv_path)
            print(f"  Loaded {num_tables} tables from INFORMATION_SCHEMA.")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    if dialect == "snowflake":
        parser = SnowflakeParser(resolver)
    else:
        parser = AnsiParser(resolver)

    # Error categories (E1–E8 from ARCHITECTURE_REVIEW.md § 12.1)
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

    # Error prefix histogram (top 20 prefixes)
    error_prefix_histogram: dict[str, int] = {}

    # Timing and success tracking
    per_file_times: list[float] = []
    slow_files: list[dict[str, Any]] = []
    success_edges = 0
    placeholder_edges = 0
    files_with_success = 0
    total_statements = 0

    # New counters for T-01–T-05 observability
    t02_fallback_attempts = 0
    t03_pure_ddl_skips = 0
    t04_recovery_primary_failures = 0
    t04_recovery_chunk_failures = 0
    scripting_block_files = 0
    star_skip = 0
    e5_statement_count = 0  # Separate bucket for statement-level E5 errors

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

        # Classify errors from the production parser's output
        file_had_success = False
        for error_msg in parsed.errors:
            # Record error prefix histogram (first two colon segments)
            prefix = error_msg.split(":")[0]
            if ":" in error_msg:
                prefix = ":".join(error_msg.split(":")[:2])
            error_prefix_histogram[prefix] = error_prefix_histogram.get(prefix, 0) + 1

            # Classify errors into E1–E8 buckets
            if error_msg.startswith("col_lineage_skip:expr_no_name"):
                # E2: expression has no resolvable name (after T-02 fallback skip)
                errors["E2"]["count"] += 1
                if len(errors["E2"]["example_files"]) < 10:
                    errors["E2"]["example_files"].append(str(file_path.relative_to(corpus_path)))
                if len(errors["E2"]["example_messages"]) < 5:
                    errors["E2"]["example_messages"].append(error_msg)

            elif error_msg.startswith("col_lineage_attempt:expr_fallback:"):
                # T-02 fallback attempt: informational, NOT an error
                t02_fallback_attempts += 1

            elif error_msg.startswith("col_lineage_skip:dynamic_source"):
                # E8: root returned but no edges from dynamic sources
                errors["E8"]["count"] += 1
                if len(errors["E8"]["example_files"]) < 10:
                    errors["E8"]["example_files"].append(str(file_path.relative_to(corpus_path)))
                if len(errors["E8"]["example_messages"]) < 5:
                    errors["E8"]["example_messages"].append(error_msg)

            elif error_msg.startswith("col_lineage_skip:star:"):
                # Star projection skip
                star_skip += 1

            elif error_msg.startswith("col_lineage:tree_walk:"):
                # E7: tree_walk failure
                errors["E7"]["count"] += 1
                if len(errors["E7"]["example_files"]) < 10:
                    errors["E7"]["example_files"].append(str(file_path.relative_to(corpus_path)))
                if len(errors["E7"]["example_messages"]) < 5:
                    errors["E7"]["example_messages"].append(error_msg)

            elif error_msg.startswith("col_lineage:statement:"):
                # E5: statement-level lineage error (separate from column-level)
                e5_statement_count += 1
                errors["E5"]["count"] += 1
                if len(errors["E5"]["example_files"]) < 10:
                    errors["E5"]["example_files"].append(str(file_path.relative_to(corpus_path)))
                if len(errors["E5"]["example_messages"]) < 5:
                    errors["E5"]["example_messages"].append(error_msg)

            elif error_msg.startswith("col_lineage:"):
                # E5: column-level lineage exception. Check for E1 pattern (qualified column with dot).
                if "Cannot find column" in error_msg and "." in error_msg:
                    # Heuristic for E1: qualified column names (db.col or table.col)
                    errors["E1"]["count"] += 1
                    if len(errors["E1"]["example_files"]) < 10:
                        errors["E1"]["example_files"].append(str(file_path.relative_to(corpus_path)))
                    if len(errors["E1"]["example_messages"]) < 5:
                        errors["E1"]["example_messages"].append(error_msg)
                else:
                    # E5: general lineage extraction error
                    errors["E5"]["count"] += 1
                    if len(errors["E5"]["example_files"]) < 10:
                        errors["E5"]["example_files"].append(str(file_path.relative_to(corpus_path)))
                    if len(errors["E5"]["example_messages"]) < 5:
                        errors["E5"]["example_messages"].append(error_msg)

            elif error_msg.startswith("parse_recovery:primary:"):
                # T-04: recovery from primary parse failure
                t04_recovery_primary_failures += 1

            elif error_msg.startswith("parse_recovery:chunk:"):
                # T-04: recovery from chunk parse failure
                t04_recovery_chunk_failures += 1

            elif error_msg.startswith("parse_mode:pure_ddl_skip"):
                # T-03: pure-DDL file early skip
                t03_pure_ddl_skips += 1

            elif error_msg.startswith("parse_mode:scripting_block"):
                # Scripting block fallback
                scripting_block_files += 1

        # Count edges from parsed.statements
        file_had_success = False
        for stmt in parsed.statements:
            for edge in stmt.column_lineage:
                if edge.confidence > 0.0:
                    success_edges += 1
                    file_had_success = True
                elif edge.confidence == 0.0:
                    placeholder_edges += 1

            total_statements += 1

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
        "runtime_seconds": round(total_time, 1),
        "error_summary": errors,
        "success_edges": success_edges,
        "placeholder_edges": placeholder_edges,
        "files_with_success": files_with_success,
        "files_ok": files_ok,
        "files_degraded": files_degraded,
        "files_failed": files_failed,
        # T-01–T-05 observability counters
        "t02_fallback_attempts": t02_fallback_attempts,
        "t03_pure_ddl_skips": t03_pure_ddl_skips,
        "t04_recovery_primary_failures": t04_recovery_primary_failures,
        "t04_recovery_chunk_failures": t04_recovery_chunk_failures,
        "scripting_block_files": scripting_block_files,
        "star_skip": star_skip,
        "e5_statement_count": e5_statement_count,
        # Error prefix histogram (top 20)
        "error_prefix_histogram": dict(
            sorted(error_prefix_histogram.items(), key=lambda x: x[1], reverse=True)[:20]
        ),
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
    print("Note: E3 measures parse-time Command warnings, NOT lineage-blocking events.")
    print("      T-03 deliberately does not reduce E3 by design.")
    print()
    n1, n2, n3, n4, n5 = (errors[k]["count"] for k in ("E1", "E2", "E3", "E4", "E5"))
    e3_files = len(set(errors["E3"]["example_files"]))
    e4_files = len(set(errors["E4"]["example_files"]))
    print(f"E1  alias_col_ref      {n1:6d} occurrences")
    print(f"E2  expr_no_name       {n2:6d} occurrences  (pure skip, not fallback)")
    print(f"E3  command_fallback   {n3:6d} occurrences (in {e3_files:3d} files)")
    print(f"E4  parse_failure      {n4:6d} occurrences ({e4_files:3d} files, full skip)")
    print(f"E5  lineage_other      {n5:6d} occurrences (statement-level: {e5_statement_count})")
    print(f"E6  schema_mismatch    {errors['E6']['count']:6d} occurrences")
    print(f"E7  tree_walk_fail     {errors['E7']['count']:6d} occurrences")
    print(f"E8  no_edges_from_root {errors['E8']['count']:6d} occurrences")

    print("\n=== T-01–T-05 Observability ===")
    print(f"T-02 fallback attempts     {t02_fallback_attempts:6d} (best-effort expr name extraction)")
    print(f"T-03 pure-DDL skips        {t03_pure_ddl_skips:6d} (early exit, no lineage)")
    print(f"T-04 recovery primary      {t04_recovery_primary_failures:6d} (Snowflake dialect recovery)")
    print(f"T-04 recovery chunk        {t04_recovery_chunk_failures:6d} (per-statement fallback)")
    print(f"Scripting blocks           {scripting_block_files:6d} (fallback mode)")
    print(f"Star projection skips      {star_skip:6d} (unresolved SELECT *)")

    print("\n=== Edge Count ===")
    total_files = len(sql_files)
    total_edges = success_edges + placeholder_edges
    pct_success = 100 * success_edges / total_edges if total_edges else 0
    print(f"Success edges (confidence > 0.0):     {success_edges:6d}")
    print(f"Placeholder edges (confidence == 0.0): {placeholder_edges:6d}")
    print(f"Total edges:                           {total_edges:6d} ({pct_success:.1f}% success)")
    pct_files = 100 * files_with_success / total_files if total_files else 0
    print(f"Files with success edges:  {files_with_success} of {total_files} ({pct_files:.1f}%)")

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
