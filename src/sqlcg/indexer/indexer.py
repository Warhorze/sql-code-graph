"""Main indexer orchestrating parsing and graph persistence."""

import multiprocessing as mp
import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlcg.core.config import get_external_consumers, get_presentation_prefixes
from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.indexer.error_classify import _classify_error, dominant_cause
from sqlcg.indexer.pool import HardKillPool
from sqlcg.indexer.walker import walk_sql_files
from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.base import ParsedFile
from sqlcg.parsers.registry import get_parser
from sqlcg.utils.ignore import load_ignore_spec
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


@dataclass
class FileRowSet:
    """Row dicts produced from a single ParsedFile (Phase A output).

    Pure data container — no db access, no execute(). Used as the unit
    passed from _build_file_rows to BatchRowBuffer.extend().
    """

    file_rows: list[dict] = field(default_factory=list)
    table_rows: list[dict] = field(default_factory=list)
    column_rows: list[dict] = field(default_factory=list)
    query_rows: list[dict] = field(default_factory=list)
    defined_in_edges: list[dict] = field(default_factory=list)
    has_column_edges: list[dict] = field(default_factory=list)
    query_defined_in_edges: list[dict] = field(default_factory=list)
    selects_from_edges: list[dict] = field(default_factory=list)
    column_lineage_edges: list[dict] = field(default_factory=list)
    star_source_edges: list[dict] = field(default_factory=list)
    counts: dict = field(
        default_factory=lambda: {"tables": 0, "edges": 0, "columns_defined": 0, "star_sources": 0}
    )
    parse_quality_key: str = "full"


@dataclass
class BatchRowBuffer:
    """Accumulated row dicts across all files in a batch.

    Used by _flush_row_batch to dedup and flush once per batch instead of
    once per file — the v1.1.1 perf improvement (~13,400 → ~270 execute()).
    """

    file_rows: list[dict] = field(default_factory=list)
    table_rows: list[dict] = field(default_factory=list)
    column_rows: list[dict] = field(default_factory=list)
    query_rows: list[dict] = field(default_factory=list)
    defined_in_edges: list[dict] = field(default_factory=list)
    has_column_edges: list[dict] = field(default_factory=list)
    query_defined_in_edges: list[dict] = field(default_factory=list)
    selects_from_edges: list[dict] = field(default_factory=list)
    column_lineage_edges: list[dict] = field(default_factory=list)
    star_source_edges: list[dict] = field(default_factory=list)

    def extend(self, rows: FileRowSet) -> None:
        """Accumulate one file's row sets into this buffer."""
        self.file_rows.extend(rows.file_rows)
        self.table_rows.extend(rows.table_rows)
        self.column_rows.extend(rows.column_rows)
        self.query_rows.extend(rows.query_rows)
        self.defined_in_edges.extend(rows.defined_in_edges)
        self.has_column_edges.extend(rows.has_column_edges)
        self.query_defined_in_edges.extend(rows.query_defined_in_edges)
        self.selects_from_edges.extend(rows.selects_from_edges)
        self.column_lineage_edges.extend(rows.column_lineage_edges)
        self.star_source_edges.extend(rows.star_source_edges)

    @classmethod
    def from_single(cls, rows: FileRowSet) -> "BatchRowBuffer":
        """Build a single-file batch buffer (used by the thin _upsert_parsed_file wrapper)."""
        buf = cls()
        buf.extend(rows)
        return buf


def _flush_row_batch(db: GraphBackend, buf: BatchRowBuffer) -> None:
    """Dedup accumulated rows across the batch, then issue the 10 bulk calls ONCE.

    This is the v1.1.1 batch-flush core: called once per batch (not once per file).
    Dedup keys mirror the graph's MERGE cardinality:
      - file_rows:             path (primary key)
      - table_rows:            qualified (primary key); prefers row with non-empty
                               defined_in_file so DEFINED_IN provenance is preserved.
      - column_rows:           id (primary key)
      - query_rows:            id (primary key, globally unique path:index)
      - edge rows:             (src_key, dst_key) only — matches MERGE (src)-[r]->(dst)
                               cardinality; KuzuDB cannot hold two edges of the same
                               type between the same pair.

    Performance invariant: calls each upsert_*_bulk at most ONCE per invocation;
    never calls upsert_node / upsert_edge per row.
    Guard: tests/unit/test_upsert_batch_invariant.py
    """
    # --- Phase B: batch-scoped dedup ---
    # For table_rows, prefer defined rows (non-empty defined_in_file) so provenance
    # is not lost when a shared table is referenced by multiple files.
    table_dedup: dict[str, dict] = {}
    for r in buf.table_rows:
        key = r["qualified"]
        existing = table_dedup.get(key)
        if existing is None or (not existing.get("defined_in_file") and r.get("defined_in_file")):
            table_dedup[key] = r
    table_rows = list(table_dedup.values())

    column_rows = list({r["id"]: r for r in buf.column_rows}.values())
    query_rows = list({r["id"]: r for r in buf.query_rows}.values())
    file_rows = list({r["path"]: r for r in buf.file_rows}.values())

    # Edge dedup by (src_key, dst_key) — matches MERGE (src)-[r]->(dst) cardinality.
    defined_in_edges = list(
        {(r["src_key"], r["dst_key"]): r for r in buf.defined_in_edges}.values()
    )
    has_column_edges = list(
        {(r["src_key"], r["dst_key"]): r for r in buf.has_column_edges}.values()
    )
    query_defined_in_edges = list(
        {(r["src_key"], r["dst_key"]): r for r in buf.query_defined_in_edges}.values()
    )
    selects_from_edges = list(
        {(r["src_key"], r["dst_key"]): r for r in buf.selects_from_edges}.values()
    )
    column_lineage_edges = list(
        {(r["src_key"], r["dst_key"]): r for r in buf.column_lineage_edges}.values()
    )
    star_source_edges = list(
        {(r["src_key"], r["dst_key"]): r for r in buf.star_source_edges}.values()
    )

    # --- Phase C: flush in dependency order (nodes before their edges) ---
    db.upsert_nodes_bulk(NodeLabel.FILE, file_rows)
    db.upsert_nodes_bulk(NodeLabel.TABLE, table_rows)
    db.upsert_nodes_bulk(NodeLabel.COLUMN, column_rows)
    db.upsert_nodes_bulk(NodeLabel.QUERY, query_rows)

    db.upsert_edges_bulk(NodeLabel.TABLE, NodeLabel.FILE, RelType.DEFINED_IN, defined_in_edges)
    db.upsert_edges_bulk(NodeLabel.TABLE, NodeLabel.COLUMN, RelType.HAS_COLUMN, has_column_edges)
    db.upsert_edges_bulk(
        NodeLabel.QUERY, NodeLabel.FILE, RelType.QUERY_DEFINED_IN, query_defined_in_edges
    )
    db.upsert_edges_bulk(NodeLabel.QUERY, NodeLabel.TABLE, RelType.SELECTS_FROM, selects_from_edges)
    db.upsert_edges_bulk(
        NodeLabel.COLUMN, NodeLabel.COLUMN, RelType.COLUMN_LINEAGE, column_lineage_edges
    )
    db.upsert_edges_bulk(NodeLabel.QUERY, NodeLabel.TABLE, RelType.STAR_SOURCE, star_source_edges)


def _subprocess_parse_worker(parser_cls, dialect, path, sql, q):
    """Parse a single file in a subprocess; queue the ParsedFile (or exception).

    parser_cls must be the *class* (pickleable), not an instance. The worker
    instantiates the parser inside the child process so that any thread-local
    state from the parent does not leak.

    T-09-04: Parser constructors require a SchemaResolver. The subprocess gets a
    fresh empty resolver; column resolution runs in infer-only mode, the same as
    small-repo mode.
    """
    try:
        parser = parser_cls(SchemaResolver(dialect=str(dialect) if dialect else None))
        out = parser.parse_file(path, sql)
        q.put(out)
    except BaseException as exc:
        # Send the exception back; parent will re-raise.
        try:
            q.put(exc)
        except Exception:
            # If exc isn't pickleable, send a RuntimeError summary instead.
            q.put(RuntimeError(f"subprocess parse failed: {type(exc).__name__}: {exc}"))


class Indexer:
    """Orchestrates SQL file parsing and graph persistence."""

    def index_repo(
        self,
        path: Path,
        dialect: str | None,
        db: GraphBackend,
        dbt_manifest: Path | None = None,
        timeout_per_file: int = 10,
        use_git: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
        no_ddl: bool = False,
        batch_size: int = 50,
        n_workers: int | None = None,
        profile: bool = False,
    ) -> dict:
        """Full two-pass index with batched writes. Returns summary dict.

        Args:
            path: Root directory to index
            dialect: SQL dialect (None for ANSI)
            db: GraphBackend instance
            dbt_manifest: Optional path to dbt manifest.json
            timeout_per_file: Timeout in seconds per file (0 = no timeout)
            use_git: When True (default), use git ls-files to restrict
                indexing to tracked files; falls back to rglob when git
                is unavailable or the directory is not a git repository.
            progress_callback: Optional callback(n, total) invoked every 100 files
            no_ddl: When True, skip DDL-only files from upsert into the graph
            batch_size: Number of files committed per KuzuDB transaction in the upsert pass.
                Higher values reduce commit overhead but increase the per-batch recovery cost
                and the working-set size when a transaction holds locks. Default 50 is a
                balance between throughput on large corpora (1,000+ files) and memory/lock
                pressure on small ones. batch_size=1 reproduces the legacy per-file commit
                behaviour. Must be >= 1.
            n_workers: Number of worker processes in the persistent pool (default: cpu_count()).
                Workers are spawn-mode so they never inherit the KuzuDB file descriptor.
                Each worker holds two pre-built parsers (one per pass), paying the
                cold-start cost once per run rather than per file.
            profile: When True, attach a "profile" dict to the returned summary with
                per-stage wall-time (pass1_parse_s, pass2_resolve_s, upsert_s,
                star_expand_s, total_s), per-file ms, and the top-10 slowest files by
                parse time. When False (default), no timing calls are made in the hot
                loop — zero overhead.

        Returns:
            Dict with keys: files_parsed, parse_errors, tables_found,
            lineage_edges_created, quality, batch_size
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        # Timing accumulators — only assigned when profile=True; zero-initialised
        # here so pyright can infer their types as float throughout the method.
        _t_pass1_start: float = 0.0
        _t_pass1_end: float = 0.0
        _t_pass2_start: float = 0.0
        _t_pass2_end: float = 0.0
        _t_upsert_start: float = 0.0
        _t_upsert_end: float = 0.0
        _t_star_start: float = 0.0
        _t_star_end: float = 0.0

        spec = load_ignore_spec(path)
        aggregator = CrossFileAggregator()

        files = list(walk_sql_files(path, spec, use_git=use_git))
        pass1_results: list[ParsedFile] = []
        parse_errors = 0
        total_files = len(files)

        # Read all SQL files up-front; unreadable files get an empty string
        # so the pool still produces a ParsedFile placeholder for them.
        file_sqls: list[tuple[Path, str]] = []
        for file_path in files:
            try:
                file_sqls.append((file_path, file_path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Failed to read %s: %s", file_path, exc)
                file_sqls.append((file_path, ""))
                parse_errors += 1

        p1_timeout = float(timeout_per_file) if timeout_per_file > 0 else float("inf")
        p2_timeout = max(p1_timeout, 15.0) if timeout_per_file > 0 else float("inf")

        from sqlcg.core.config import get_schema_aliases

        schema_aliases = get_schema_aliases(path)

        p1_tasks = [{"type": "parse_pass1", "path": str(fp), "sql": sql} for fp, sql in file_sqls]

        p1_done = 0
        # Per-file parse times collected when profile=True.
        # Each entry is (path_str, elapsed_ms) recorded at result-delivery time.
        # CAVEAT (W1): this measures the wall time between consecutive worker-result
        # deliveries to the coordinator, NOT the true in-worker parse duration. It
        # reflects coordinator receive-order and scheduling overhead, so `slowest_files`
        # is a proxy for "files whose results arrived after the longest gap", not an
        # absolute per-file parse-latency ranking. Per-stage totals below ARE accurate.
        # A true per-file timing would require worker-side start/end timestamps returned
        # with each ParsedFile (follow-up; would touch pool.py).
        _file_parse_times: list[tuple[str, float]] = []
        _last_result_t: list[float] = []  # single-element list used as a nonlocal cell

        if profile:
            _last_result_t.append(time.perf_counter())

        def _p1_on_result() -> None:
            nonlocal p1_done
            p1_done += 1
            if profile:
                now = time.perf_counter()
                elapsed_ms = (now - _last_result_t[0]) * 1000.0
                # Associate elapsed time with the file that just completed.
                # p1_done-1 is the 0-based index of the just-completed file.
                file_idx = p1_done - 1
                if file_idx < len(file_sqls):
                    _file_parse_times.append((str(file_sqls[file_idx][0]), elapsed_ms))
                _last_result_t[0] = now
            if progress_callback is not None:
                progress_callback(p1_done, total_files)

        if profile:
            _t_pass1_start = time.perf_counter()

        try:
            with HardKillPool(dialect, n_workers, schema_aliases=schema_aliases) as pool:
                # Pass 1: parse all files in parallel
                p1_raw = pool.map(
                    p1_tasks,
                    per_task_timeout=p1_timeout,
                    on_result=_p1_on_result,
                )

                if profile:
                    _t_pass1_end = time.perf_counter()

                # Register pass-1 results with the aggregator
                for i, (file_path, _sql) in enumerate(file_sqls):
                    raw = p1_raw[i]
                    if raw is None:
                        parsed = ParsedFile(path=file_path, dialect=dialect)
                        parsed.errors.append("pool_no_result")
                        parse_errors += 1
                    else:
                        parsed = raw
                        parse_errors += len(parsed.errors)
                    aggregator.register_pass1(parsed)
                    pass1_results.append(parsed)

                # Optional: load dbt manifest into a transient resolver so that
                # cross-file sources can reference dbt-managed tables.
                # Note: dbt schema data is not propagated to pool workers; pass-2
                # workers run with the information-schema CSV only.
                if dbt_manifest:
                    from sqlcg.indexer.dbt_adapter import load_dbt_manifest

                    _dbt_resolver = SchemaResolver(dialect=dialect)
                    load_dbt_manifest(dbt_manifest, _dbt_resolver)

                # Seed cross-file CTAS bodies into the aggregator (main process only;
                # workers receive per-task SQL string copies).
                xfile_sources = aggregator.cross_file_sources

                # Build pass-2 tasks: only files with cross-file dependencies
                pass2_indices: list[int] = []
                p2_tasks: list[dict] = []
                for i, parsed in enumerate(pass1_results):
                    if not aggregator._needs_pass2(parsed):
                        continue
                    dep_names = {(t.name or "").lower() for t in parsed.referenced_tables if t.name}
                    # Serialize only the CTAS bodies this file needs (keeps payload small)
                    xfile_sql: dict[str, str] = {}
                    for name, body in xfile_sources.items():
                        if name in dep_names:
                            try:
                                xfile_sql[name] = body.sql(dialect=dialect)
                            except Exception as exc:
                                logger.warning(
                                    "Failed to serialize cross-file source %s: %s", name, exc
                                )
                    pass2_indices.append(i)
                    p2_tasks.append(
                        {
                            "type": "parse_pass2",
                            "path": str(parsed.path),
                            "sql": file_sqls[i][1],
                            "dependency_filter": dep_names,
                            "xfile_sql": xfile_sql,
                        }
                    )

                pass2_skipped = len(pass1_results) - len(pass2_indices)

                if profile:
                    _t_pass2_start = time.perf_counter()

                # Pass 2: resolve cross-file references in parallel
                p2_raw = pool.map(p2_tasks, per_task_timeout=p2_timeout)

                if profile:
                    _t_pass2_end = time.perf_counter()

        except KeyboardInterrupt:
            # Kill workers and abort immediately. A partial pass-1-only result is
            # an incomplete graph (no cross-file resolution, no star expansion);
            # writing it would leave a misleading half-index. Re-run `sqlcg index`
            # to index — re-indexing is the migration path.
            logger.warning("Interrupted — workers killed; no partial graph written.")
            raise

        # Assemble final pass-2 results: start from pass-1, overlay pass-2 where available
        pass2_results: list[ParsedFile] = list(pass1_results)
        for k, orig_idx in enumerate(pass2_indices):
            r = p2_raw[k]
            if r is None or isinstance(r, BaseException):
                logger.warning(
                    "Pass-2 failed for %s (%s) — keeping pass-1 result",
                    pass1_results[orig_idx].path,
                    r,
                )
            else:
                pass2_results[orig_idx] = r

        # Build duplicate-DDL registry from in-memory results so _upsert_parsed_file
        # can detect conflicts with a dict lookup instead of a per-table graph read.
        defined_table_registry: dict[str, str] = {}
        for pf in pass2_results:
            for table in pf.defined_tables:
                defined_table_registry.setdefault(table.full_id, pf.path_str)

        # Upsert all results and count quality distribution using batched transactions
        nonlocal_counts: dict = {
            "tables": 0,
            "edges": 0,
            "star_sources": 0,
            "columns_defined": 0,
            "quality": {"full": 0, "table_only": 0, "scripting_fallback": 0, "failed": 0},
        }

        def _flush_batch(batch: list[ParsedFile]) -> None:
            """Accumulate rows for the batch and flush once (v1.1.1 batch-upsert path).

            Per-file row-building errors are isolated inside _upsert_file_batch; good
            files always flush together in a single transaction.

            Note: sqlcg watch's reindex_file uses a separate code path with
            its own short per-file transaction. PERF-BATCH only affects index_repo.
            """
            self._upsert_file_batch(batch, db, defined_table_registry, nonlocal_counts)

        if profile:
            _t_upsert_start = time.perf_counter()

        batch: list[ParsedFile] = []
        for parsed in pass2_results:
            skip_upsert = no_ddl and any("pure_ddl_skip" in e for e in parsed.errors)
            if skip_upsert:
                nonlocal_counts["quality"]["scripting_fallback"] += 1
                continue
            batch.append(parsed)
            if len(batch) >= batch_size:
                _flush_batch(batch)
                batch = []  # IMPORTANT — free the references so GC can reclaim parsed.statements
        _flush_batch(batch)  # final partial batch

        if profile:
            _t_upsert_end = time.perf_counter()
            _t_star_start = time.perf_counter()

        # Post-ingestion: expand STAR_SOURCE edges into concrete COLUMN_LINEAGE edges
        star_edges_expanded = self._expand_star_sources(db)

        if profile:
            _t_star_end = time.perf_counter()

        # Post-ingestion: ingest declared external downstream consumers from .sqlcg.toml
        ingest_result = self._ingest_external_consumers(db, path)

        # Persist the HEAD SHA so `sqlcg reindex` (no SHAs) can compute the delta.
        # Silent on failure — non-git repos still index cleanly.
        try:
            import subprocess as _sp

            _sha_result = _sp.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(path),
                capture_output=True,
                text=True,
            )
            if _sha_result.returncode == 0:
                _head_sha = _sha_result.stdout.strip()
                if _head_sha:
                    db.set_indexed_sha(_head_sha)
        except Exception as _sha_exc:
            logger.debug("Could not write indexed_sha: %s", _sha_exc)

        # Classify all errors into buckets for measurement and reporting
        error_summary: dict[str, int] = {
            "E1": 0,
            "E2": 0,
            "E3": 0,
            "E5": 0,
            "E8": 0,
            "timeout": 0,
            "pure_ddl_skip": 0,
            "func_fallback": 0,
            "qualify_failed": 0,
            "other": 0,
        }
        for parsed in pass2_results:
            for msg in parsed.errors:
                bucket = _classify_error(msg)
                if bucket in error_summary:
                    error_summary[bucket] += 1

        # Emit summary log line
        summary_parts = [f"{k}: {v}" for k, v in error_summary.items() if v > 0]
        logger.info(
            "Indexing complete: %d files — %s",
            len(pass2_results),
            ", ".join(summary_parts) if summary_parts else "(no errors)",
        )

        result: dict = {
            "files_parsed": len(pass2_results),
            "pass2_skipped": pass2_skipped,
            "parse_errors": parse_errors,
            "tables_found": nonlocal_counts["tables"],
            "lineage_edges_created": nonlocal_counts["edges"],
            "columns_defined": nonlocal_counts["columns_defined"],
            "star_sources": nonlocal_counts["star_sources"],
            "star_edges_expanded": star_edges_expanded,
            "quality": nonlocal_counts["quality"],
            "error_summary": error_summary,
            "batch_size": batch_size,
            "external_consumers": ingest_result["consumers"],
            "external_consumer_edges": ingest_result["edges"],
            "external_consumer_warnings": ingest_result["warnings"],
        }

        if profile:
            pass1_s = _t_pass1_end - _t_pass1_start
            pass2_s = _t_pass2_end - _t_pass2_start
            upsert_s = _t_upsert_end - _t_upsert_start
            star_s = _t_star_end - _t_star_start
            total_s = pass1_s + pass2_s + upsert_s + star_s
            n_files = max(len(pass2_results), 1)
            slowest = sorted(_file_parse_times, key=lambda x: x[1], reverse=True)[:10]
            result["profile"] = {
                "pass1_parse_s": pass1_s,
                "pass2_resolve_s": pass2_s,
                "upsert_s": upsert_s,
                "star_expand_s": star_s,
                "total_s": total_s,
                "files": len(pass2_results),
                "ms_per_file": total_s / n_files * 1000.0,
                "slowest_files": slowest,
            }

        return result

    def resync_changed(
        self,
        root: Path,
        old_sha: str,
        new_sha: str,
        db: GraphBackend,
        dialect: str | None,
        *,
        batch_size: int = 50,
        timeout_per_file: int = 10,
        max_closure_depth: int = 3,
    ) -> dict:
        """Incrementally resync the graph after a git branch change or pull.

        Only re-parses files in the git delta plus the cross-file pass-2
        affected closure (files that SELECT_FROM tables defined in changed/deleted
        files). Avoids a full corpus reindex for small branch deltas.

        Algorithm (see plan/living_codebase_resync.md):
          1. Compute git delta (added/modified/deleted .sql files).
          2. Capture deleted files' table names BEFORE deletion.
          3. Delete nodes for deleted files.
          4. Pass-1 parse reparse_set (added+modified) via _index_single_file.
          5. Re-harvest CTAS bodies for frontier definer files not already parsed.
          6. Compute bounded closure (DEPENDENT_FILES_OF_TABLES, max_closure_depth).
          7. Pass-2 re-resolve closure files + reparse_set, then bulk upsert.
          8. Run _expand_star_sources once.
          9. Write new_sha to metadata on success.

        Falls back to full index_repo when:
          - git_name_status_delta returns None (git unavailable / bad SHA).
          - The closure frontier is still growing when max_closure_depth is reached.
        In both fallback cases, fell_back_to_full=True is set in the summary.

        Args:
            root:              Repository root directory.
            old_sha:           Previously-indexed git commit SHA.
            new_sha:           Current git commit SHA.
            db:                GraphBackend instance.
            dialect:           SQL dialect (None for ANSI).
            batch_size:        Files per KuzuDB transaction (same default as index_repo).
            timeout_per_file:  Per-file parse timeout in seconds for _index_single_file.
            max_closure_depth: Maximum BFS depth for closure walk; exceeding triggers
                               full-index fallback.

        Returns:
            Dict with keys: added, modified, deleted, closure_resolved,
            fell_back_to_full, batch_size.
        """
        from sqlcg.core.queries import (
            DEPENDENT_FILES_OF_TABLES_QUERY,
            GET_TABLES_DEFINED_IN_FILE_QUERY,
        )
        from sqlcg.indexer.git_delta import git_name_status_delta

        summary: dict = {
            "added": 0,
            "modified": 0,
            "deleted": 0,
            "closure_resolved": 0,
            "fell_back_to_full": False,
            "batch_size": batch_size,
        }

        # ---- Step 1: Compute delta -----------------------------------------------
        delta = git_name_status_delta(root, old_sha, new_sha)
        if delta is None:
            logger.warning(
                "resync_changed: git_name_status_delta returned None for %s..%s — "
                "falling back to full index_repo",
                old_sha[:8],
                new_sha[:8],
            )
            summary["fell_back_to_full"] = True
            self.index_repo(
                root, dialect, db, batch_size=batch_size, timeout_per_file=timeout_per_file
            )
            return summary

        reparse_set: set[Path] = delta.added | delta.modified
        delete_set: set[Path] = delta.deleted

        summary["added"] = len(delta.added)
        summary["modified"] = len(delta.modified)
        summary["deleted"] = len(delta.deleted)

        # Nothing to do — delta is empty but we still update the SHA.
        if not reparse_set and not delete_set:
            logger.debug(
                "resync_changed: empty delta %s..%s — no files to resync", old_sha[:8], new_sha[:8]
            )
            db.set_indexed_sha(new_sha)
            return summary

        # ---- Step 2: Capture deleted tables BEFORE deletion ---------------------
        deleted_tables: list[str] = []
        for del_path in delete_set:
            rows = db.run_read(GET_TABLES_DEFINED_IN_FILE_QUERY, {"file_path": str(del_path)})
            deleted_tables.extend(row["table_qualified"] for row in rows)

        # ---- Step 3: Delete nodes for deleted files --------------------------------
        with db.transaction():
            for del_path in delete_set:
                db.delete_nodes_for_file(str(del_path))

        # ---- Step 4: Pass-1 parse reparse_set via _index_single_file --------------
        # B-1 mandate: use _index_single_file (subprocess isolation + per-file timeout),
        # NOT HardKillPool (pool startup cost exceeds parsing cost for small deltas).
        aggregator = CrossFileAggregator()
        schema_resolver = SchemaResolver(dialect=dialect)
        parser = get_parser(dialect, schema_resolver)

        pass1_results: list[ParsedFile] = []
        for file_path in reparse_set:
            try:
                sql = file_path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning("resync_changed: cannot read %s: %s — skipping", file_path, exc)
                placeholder = ParsedFile(path=file_path, dialect=dialect)
                placeholder.errors.append(f"read_error:{exc}")
                aggregator.register_pass1(placeholder)
                pass1_results.append(placeholder)
                continue
            try:
                parsed = self._index_single_file(parser, file_path, sql, timeout_per_file)
            except Exception as exc:
                logger.warning("resync_changed: parse failed %s: %s", file_path, exc)
                parsed = ParsedFile(path=file_path, dialect=dialect)
                parsed.errors.append(f"parse_error:{exc}")
            aggregator.register_pass1(parsed)
            pass1_results.append(parsed)

        # ---- Step 5: Compute closure via BFS over DEPENDENT_FILES_OF_TABLES ------
        # Seed frontier with tables defined in reparse_set + deleted_tables.
        frontier_tables: list[str] = list(deleted_tables)
        for pf in pass1_results:
            frontier_tables.extend(t.full_id for t in pf.defined_tables)

        # Remove duplicates while preserving order
        seen_tables: set[str] = set(frontier_tables)
        frontier_tables = list({t: None for t in frontier_tables}.keys())

        # Already-visited files (don't re-resolve the same file twice)
        visited_files: set[str] = {str(p) for p in reparse_set} | {str(p) for p in delete_set}
        closure_files: set[str] = set()

        fell_back = False
        for depth in range(max_closure_depth):
            if not frontier_tables:
                break
            rows = db.run_read(DEPENDENT_FILES_OF_TABLES_QUERY, {"tables": frontier_tables})
            new_files: list[str] = [row["path"] for row in rows if row["path"] not in visited_files]
            if not new_files:
                break

            # Check if frontier is still growing at the depth cap
            if depth == max_closure_depth - 1:
                # Frontier still producing new files at the last allowed depth —
                # fall back to full reindex (correctness guarantee).
                logger.warning(
                    "resync_changed: closure frontier still growing at depth %d — "
                    "falling back to full index_repo",
                    max_closure_depth,
                )
                fell_back = True
                break

            for fp in new_files:
                if fp not in visited_files:
                    closure_files.add(fp)
                    visited_files.add(fp)

            # Advance frontier: tables defined in these newly-found files
            next_tables: list[str] = []
            for fp in new_files:
                t_rows = db.run_read(GET_TABLES_DEFINED_IN_FILE_QUERY, {"file_path": fp})
                next_tables.extend(row["table_qualified"] for row in t_rows)
            new_frontier = [t for t in next_tables if t not in seen_tables]
            seen_tables.update(new_frontier)
            frontier_tables = new_frontier

        if fell_back:
            summary["fell_back_to_full"] = True
            self.index_repo(
                root, dialect, db, batch_size=batch_size, timeout_per_file=timeout_per_file
            )
            return summary

        summary["closure_resolved"] = len(closure_files)

        # ---- Step 5b: Re-harvest CTAS bodies for definer files the closure needs ---
        # W-2: only look up definers for referenced tables NOT already satisfied in-memory
        # (from reparse_set pass-1). Deleted definers return no row — register nothing.
        in_memory_bare_names: set[str] = set()
        for _pf in pass1_results:
            for _stmt in _pf.statements:
                if _stmt.target and _stmt.target.name:
                    in_memory_bare_names.add(_stmt.target.name.lower())
        # Collect all bare table names referenced by closure files (need their CTAS bodies)
        closure_ref_tables: set[str] = set()
        for cl_path in closure_files:
            try:
                cl_rows = db.run_read(
                    "MATCH (f:File {path: $path})<-[:QUERY_DEFINED_IN]-(q:SqlQuery) "
                    "MATCH (q)-[:SELECTS_FROM]->(t:SqlTable) "
                    "RETURN DISTINCT t.name AS name",
                    {"path": cl_path},
                )
                for row in cl_rows:
                    bare = (row["name"] or "").lower()
                    if bare and bare not in in_memory_bare_names:
                        closure_ref_tables.add(bare)
            except Exception as exc:
                logger.debug("resync_changed: could not query refs for %s: %s", cl_path, exc)

        # For each unsatisfied referenced table, look up its defining file and parse it
        # (pass-1 only — to harvest CTAS body; do NOT upsert unless in reparse_set).
        harvested_definer_paths: set[str] = set()
        for bare_name in closure_ref_tables:
            # Try to find a defining table by bare name (approximate match on qualified)
            # We look up via GET_TABLE_DEFINING_FILES using the bare name as the qualified key
            # first, but we do a broader search since the table might be qualified differently.
            try:
                def_rows = db.run_read(
                    "MATCH (t:SqlTable)-[:DEFINED_IN]->(f:File) "
                    "WHERE t.name = $name "
                    "RETURN DISTINCT f.path AS file_path",
                    {"name": bare_name.upper()},
                )
                if not def_rows:
                    # Try lowercase match
                    def_rows = db.run_read(
                        "MATCH (t:SqlTable)-[:DEFINED_IN]->(f:File) "
                        "WHERE t.name = $name "
                        "RETURN DISTINCT f.path AS file_path",
                        {"name": bare_name},
                    )
                for row in def_rows:
                    definer_fp = row["file_path"]
                    if definer_fp in visited_files or definer_fp in harvested_definer_paths:
                        continue
                    harvested_definer_paths.add(definer_fp)
                    try:
                        def_path = Path(definer_fp)
                        def_sql = def_path.read_text(encoding="utf-8")
                        def_parsed = self._index_single_file(
                            parser, def_path, def_sql, timeout_per_file
                        )
                        # Harvest only — register for cross_file_sources but do NOT upsert
                        aggregator.register_pass1(def_parsed)
                    except Exception as exc:
                        logger.debug(
                            "resync_changed: could not harvest definer %s: %s", definer_fp, exc
                        )
            except Exception as exc:
                logger.debug("resync_changed: definer lookup for %r failed: %s", bare_name, exc)

        # Seed cross-file CTAS bodies into the schema resolver
        schema_resolver.register_cross_file_sources(aggregator.cross_file_sources)
        schema_resolver.add_view_sources(aggregator.sources)

        # ---- Step 6: Pass-2 re-resolve closure files (in-process, no subprocess) --
        # Closure files: re-parse with cross_file_sources seeded; then upsert.
        closure_results: list[ParsedFile] = []
        for cl_path_str in closure_files:
            cl_path = Path(cl_path_str)
            try:
                cl_sql = cl_path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning(
                    "resync_changed: cannot read closure file %s: %s — skipping", cl_path, exc
                )
                continue
            try:
                cl_parsed = parser.parse_file(cl_path, cl_sql)
            except Exception as exc:
                logger.warning("resync_changed: parse failed for closure file %s: %s", cl_path, exc)
                cl_parsed = ParsedFile(path=cl_path, dialect=dialect)
                cl_parsed.errors.append(f"parse_error:{exc}")
            closure_results.append(cl_parsed)

        # ---- Step 7: Batched bulk upsert (same _flush_batch path as index_repo) ----
        all_results = pass1_results + closure_results

        # Build a registry for duplicate DDL detection
        defined_table_registry: dict[str, str] = {}
        for pf in all_results:
            for table in pf.defined_tables:
                defined_table_registry.setdefault(table.full_id, pf.path_str)

        # Delete old nodes for reparsed files before upserting fresh data
        with db.transaction():
            for pf in pass1_results:
                db.delete_nodes_for_file(pf.path_str)
            for pf in closure_results:
                db.delete_nodes_for_file(pf.path_str)

        # Flush in batches using the shared _upsert_file_batch path (v1.1.1 batch-flush).
        # resync_changed does not surface tables/edges/quality counts in its summary, so
        # a temporary counts dict is passed and discarded after each batch.
        _resync_counts: dict = {
            "tables": 0,
            "edges": 0,
            "star_sources": 0,
            "columns_defined": 0,
            "quality": {"full": 0, "table_only": 0, "scripting_fallback": 0, "failed": 0},
        }
        batch: list[ParsedFile] = []
        for pf in all_results:
            batch.append(pf)
            if len(batch) >= batch_size:
                self._upsert_file_batch(
                    batch, db, defined_table_registry, _resync_counts, "resync_changed: "
                )
                batch = []
        if batch:
            self._upsert_file_batch(
                batch, db, defined_table_registry, _resync_counts, "resync_changed: "
            )

        # ---- Step 8: Single star expansion (same as index_repo) ------------------
        self._expand_star_sources(db)

        # ---- Step 9: Persist new SHA on success ----------------------------------
        db.set_indexed_sha(new_sha)

        return summary

    def reindex_file(self, file_path: str, db: GraphBackend, dialect: str | None) -> None:
        """Re-index a single file.

        Re-indexes only the changed file. The stale-view cascade that was here
        previously was a no-op (the dead query matched kind='VIEW' which is never
        written). It has been removed in PR-3 (#33). Full re-index
        (db reset && sqlcg index) is the migration path for VIEW dependency
        tracking, deferred to v1.2.

        Args:
            file_path: Path to the file to re-index
            db: GraphBackend instance
            dialect: SQL dialect (None for ANSI)
        """
        with db.transaction():
            db.delete_nodes_for_file(file_path)
            schema_resolver = SchemaResolver(dialect=dialect)
            parser = get_parser(dialect, schema_resolver)
            sql = Path(file_path).read_text(encoding="utf-8")
            parsed = parser.parse_file(Path(file_path), sql)

            self._upsert_parsed_file(parsed, db)

        # Re-run star expansion after re-indexing
        self._expand_star_sources(db)

    def _index_single_file(self, parser, path: Path, sql: str, timeout: int) -> ParsedFile:
        """Parse one file, with optional timeout via subprocess isolation.

        T-09-04: Subprocess isolation via multiprocessing.Process + spawn context.
        When timeout > 0, runs the parser in a separate OS process and sends SIGKILL
        if the timeout fires. This is the only reliable way to hard-terminate a
        long-running parser that might be stuck in sqlglot's qualify() or lineage
        calls. For timeout <= 0, runs in-process (same as the parent).

        Args:
            parser: SqlParser instance
            path: Path to the file
            sql: SQL text
            timeout: Timeout in seconds (0 = no timeout)

        Returns:
            ParsedFile with parse_failed flag set if timeout occurs
        """
        if timeout <= 0:
            return parser.parse_file(path, sql)

        ctx = mp.get_context("spawn")  # avoid fork-inherit pitfalls (KuzuDB connection FD etc.)
        # Unbounded queue: the child writes one large ParsedFile (192–552 KB pickled).
        # Using maxsize=1 with proc.join()-before-q.get() deadlocks when the result
        # exceeds the OS pipe buffer (~64 KB) — child blocks at exit waiting for the
        # parent to drain the pipe, while the parent blocks on join() waiting for the
        # child to exit. Fix: read from the queue FIRST (drives the pipe drain), then join.
        q: mp.Queue = ctx.Queue()
        proc = ctx.Process(
            target=_subprocess_parse_worker,
            args=(parser.__class__, parser.DIALECT, path, sql, q),
            daemon=True,
        )
        proc.start()

        # q.get() with the full timeout both bounds wall time and drains the pipe so
        # the child can exit cleanly. proc.join() after is just cleanup.
        try:
            result = q.get(timeout=timeout)
        except queue.Empty:
            out = ParsedFile(path=path, dialect=parser.DIALECT)
            if proc.is_alive():
                # Hard kill — this is the whole point of T-09-04.
                proc.kill()  # SIGKILL on POSIX; TerminateProcess on Windows
                proc.join(timeout=5)
                logger.warning("Timeout parsing %s (>%ds) — subprocess killed", path, timeout)
                out.errors.append(
                    f"timeout:{timeout}s"
                    f" file={path.name}"
                    f" size={len(sql)}B"
                    f" dialect={parser.DIALECT or 'ansi'}"
                )
            else:
                logger.warning("Subprocess for %s exited but produced no result", path)
                out.errors.append("subprocess_empty_result")
            return out

        proc.join(timeout=5)  # cleanup; child should have exited by now
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)

        if isinstance(result, BaseException):
            # Worker raised — re-raise into the caller (matches pre-T-09-04 thread behaviour).
            raise result
        return result

    @staticmethod
    def _qid(full_id: str) -> str:
        """Canonical graph key for a table/column: lowercased full_id."""
        return full_id.lower()

    def _build_file_rows(
        self,
        parsed: ParsedFile,
        defined_table_registry: dict[str, str] | None = None,
    ) -> FileRowSet:
        """Build all row dicts for one ParsedFile — Phase A (pure, no db access).

        Extracts the four node-row lists and six edge-row lists from a ParsedFile.
        No execute(), no db argument. A build-time exception here is caught by the
        per-file try/except in _upsert_file_batch so the bad file is isolated from
        the rest of the batch.

        Args:
            parsed: ParsedFile to build rows for
            defined_table_registry: Optional cross-file DDL dedup registry

        Returns:
            FileRowSet with all row lists and per-file counts/quality key
        """
        rows = FileRowSet()
        rows.parse_quality_key = parsed.parse_quality.value.lower()

        # File node
        cause, failed = dominant_cause(parsed.errors)
        rows.file_rows.append(
            {
                "path": parsed.path_str,
                "dialect": parsed.dialect or "",
                "parse_failed": failed,
                "parse_cause": cause,
            }
        )

        # Defined tables
        for table in parsed.defined_tables:
            # Guard: detect duplicate DDL for the same table across files
            if defined_table_registry is not None:
                first_file = defined_table_registry.get(table.full_id)
            else:
                first_file = None
            if first_file and first_file != parsed.path_str:
                logger.debug(
                    "Table %s already defined in %s — %s will add columns to the union; "
                    "star expansion may include columns from the earlier DDL file",
                    table.full_id,
                    first_file,
                    parsed.path_str,
                )
                if hasattr(parsed, "errors"):
                    parsed.errors.append(f"duplicate_ddl:{table.full_id}:already_in:{first_file}")

            rows.table_rows.append(
                {
                    "qualified": table.full_id,
                    "name": table.name,
                    "catalog": table.catalog or "",
                    "db": table.db or "",
                    "kind": "table",
                    "defined_in_file": parsed.path_str,
                }
            )
            rows.defined_in_edges.append({"src_key": table.full_id, "dst_key": parsed.path_str})
            rows.counts["tables"] += 1

        # Build a map of target.full_id -> QueryNode for column lookup
        defined_by_query = {
            s.target.full_id: s
            for s in parsed.statements
            if s.target and s.kind in ("CREATE_TABLE", "CREATE_VIEW") and s.defined_columns
        }

        # Compute set of defined table IDs for quick lookup
        defined_table_ids = {t.full_id for t in parsed.defined_tables}

        # DDL columns and HAS_COLUMN edges
        for table in parsed.defined_tables:
            qnode = defined_by_query.get(table.full_id)
            if not qnode:
                continue

            for col_name in qnode.defined_columns:
                col_id = f"{table.full_id}.{col_name}"
                rows.column_rows.append(
                    {
                        "id": col_id,
                        "col_name": col_name,
                        "table_qualified": table.full_id,
                        "catalog": table.catalog or "",
                        "db": table.db or "",
                        "table_name": table.name,
                    }
                )
                rows.has_column_edges.append(
                    {"src_key": table.full_id, "dst_key": col_id, "source": "ddl"}
                )
                rows.counts["columns_defined"] += 1

        # Query nodes and related edges
        for i, stmt in enumerate(parsed.statements):
            query_id = f"{parsed.path_str}:{i}"
            rows.query_rows.append(
                {
                    "id": query_id,
                    "file_path": parsed.path_str,
                    "statement_index": i,
                    "sql": stmt.sql[:500],
                    "kind": stmt.kind,
                    "target_table": stmt.target.full_id if stmt.target else "",
                    "parse_failed": stmt.parse_failed,
                    "confidence": stmt.confidence,
                    "parsing_mode": stmt.parsing_mode,
                    "start_line": stmt.start_line,
                }
            )
            rows.query_defined_in_edges.append({"src_key": query_id, "dst_key": parsed.path_str})

            # Source table edges — apply structural role from TableRef.role.
            # Real source tables have role="table"; CTE aliases have role="cte".
            # Exclude <output> synthetic sinks (fallback dst when stmt.target is None).
            for src_table in stmt.sources:
                if src_table.full_id == "<output>":
                    continue
                rows.table_rows.append(
                    {
                        "qualified": src_table.full_id,
                        "name": src_table.name,
                        "catalog": src_table.catalog or "",
                        "db": src_table.db or "",
                        "kind": src_table.role,
                        "defined_in_file": "",
                    }
                )
                rows.selects_from_edges.append({"src_key": query_id, "dst_key": src_table.full_id})

            # Column lineage edges.
            # CTE destination tables (role="cte") are not in stmt.sources so they are not
            # emitted by the source-table loop above.  Collect them here from edge.dst so
            # that each CTE alias gets a SqlTable node with kind="cte" in the graph.
            # Also exclude <output> synthetic sinks from both table_rows and column_rows.
            for edge in stmt.column_lineage:
                src_id = edge.src.full_id
                dst_id = edge.dst.full_id
                # Exclude <output> sink — it is a synthetic fallback, not a real table
                if edge.dst.table.full_id == "<output>":
                    continue
                rows.column_rows.append(
                    {
                        "id": src_id,
                        "col_name": edge.src.name,
                        "table_qualified": edge.src.table.full_id,
                        "catalog": edge.src.table.catalog or "",
                        "db": edge.src.table.db or "",
                        "table_name": edge.src.table.name,
                    }
                )
                rows.column_rows.append(
                    {
                        "id": dst_id,
                        "col_name": edge.dst.name,
                        "table_qualified": edge.dst.table.full_id,
                        "catalog": edge.dst.table.catalog or "",
                        "db": edge.dst.table.db or "",
                        "table_name": edge.dst.table.name,
                    }
                )
                # Emit CTE destination table nodes so they appear as SqlTable with kind="cte"
                if edge.dst.table.role == "cte":
                    rows.table_rows.append(
                        {
                            "qualified": edge.dst.table.full_id,
                            "name": edge.dst.table.name,
                            "catalog": edge.dst.table.catalog or "",
                            "db": edge.dst.table.db or "",
                            "kind": "cte",
                            "defined_in_file": "",
                        }
                    )
                rows.column_lineage_edges.append(
                    {
                        "src_key": src_id,
                        "dst_key": dst_id,
                        "transform": edge.transform,
                        "confidence": edge.confidence,
                        "query_id": query_id,
                    }
                )
                rows.counts["edges"] += 1

            # STAR_SOURCE edges for graph-backend expansion — real source tables, keep kind="table"
            for star in stmt.star_sources:
                rows.table_rows.append(
                    {
                        "qualified": star.source.full_id,
                        "name": star.source.name,
                        "catalog": star.source.catalog or "",
                        "db": star.source.db or "",
                        "kind": "table",
                        "defined_in_file": "",
                    }
                )
                rows.star_source_edges.append(
                    {
                        "src_key": query_id,
                        "dst_key": star.source.full_id,
                        "qualifier": star.qualifier or "<unqualified>",
                        "target_table": stmt.target.full_id if stmt.target else "",
                        "confidence": 0.8,
                    }
                )
                rows.counts["star_sources"] += 1

            # Upsert target table node (if not already a defined_table)
            # so that star expansion can create destination columns
            if stmt.target and stmt.target.full_id not in defined_table_ids:
                rows.table_rows.append(
                    {
                        "qualified": stmt.target.full_id,
                        "name": stmt.target.name,
                        "catalog": stmt.target.catalog or "",
                        "db": stmt.target.db or "",
                        "kind": "table",
                        "defined_in_file": "",
                    }
                )

        return rows

    def _upsert_file_batch(
        self,
        batch: list[ParsedFile],
        db: GraphBackend,
        defined_table_registry: dict[str, str],
        nonlocal_counts: dict,
        warning_prefix: str = "",
    ) -> None:
        """Accumulate rows for all files in batch, then flush once in one transaction.

        Per-file failure isolation: _build_file_rows (pure) runs per file inside its
        own try/except — a bad file is recorded as failed and excluded from the buffer;
        the remaining good files flush together. Flush-time backend errors (rare) fail
        the whole batch transaction, which is already the behaviour inside db.transaction().

        Args:
            batch: List of ParsedFile objects to upsert
            db: GraphBackend instance
            defined_table_registry: Cross-file DDL dedup registry
            nonlocal_counts: Mutable summary dict updated in place (tables/edges/quality/…)
            warning_prefix: Optional prefix for warning log messages (e.g. "resync_changed: ")
        """
        if not batch:
            return
        buf = BatchRowBuffer()
        for parsed_in_batch in batch:
            try:
                file_rows = self._build_file_rows(parsed_in_batch, defined_table_registry)
            except Exception as exc:
                logger.warning(
                    "%sFailed to build rows for %s: %s — skipping",
                    warning_prefix,
                    parsed_in_batch.path,
                    exc,
                )
                nonlocal_counts["quality"]["failed"] += 1
                continue
            buf.extend(file_rows)
            nonlocal_counts["tables"] += file_rows.counts["tables"]
            nonlocal_counts["edges"] += file_rows.counts["edges"]
            nonlocal_counts["star_sources"] += file_rows.counts.get("star_sources", 0)
            nonlocal_counts["columns_defined"] += file_rows.counts.get("columns_defined", 0)
            nonlocal_counts["quality"][file_rows.parse_quality_key] += 1
        with db.transaction():
            _flush_row_batch(db, buf)

    def _upsert_parsed_file(
        self,
        parsed: ParsedFile,
        db: GraphBackend,
        defined_table_registry: dict[str, str] | None = None,
    ) -> dict:
        """Thin single-file wrapper used by reindex_file and sqlcg watch.

        Builds one file's rows (pure) and flushes them immediately — semantically
        equivalent to the old per-file upsert, but now via the shared builder/flusher
        split. The caller (reindex_file at indexer.py:796) wraps this in its own
        db.transaction(); this method does NOT open a transaction.

        Args:
            parsed: ParsedFile to upsert
            db: GraphBackend instance
            defined_table_registry: Optional cross-file DDL dedup registry

        Returns:
            Dict with keys: tables, edges, columns_defined, star_sources
        """
        file_rows = self._build_file_rows(parsed, defined_table_registry)
        buf = BatchRowBuffer.from_single(file_rows)
        _flush_row_batch(db, buf)
        return file_rows.counts

    def _expand_star_sources(self, db: GraphBackend) -> int:
        """Run the post-ingestion star expansion query.

        Returns:
            Number of COLUMN_LINEAGE edges created by the expansion
        """
        from sqlcg.core.queries import EXPAND_STAR_SOURCES_QUERY

        # Count COLUMN_LINEAGE edges before expansion
        before = db.run_read(
            "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN count(r) AS n",
            {},
        )
        before_count = before[0]["n"] if before else 0

        # Run the expansion query (without explicit transaction, as caller may already be in one)
        db.run_read(EXPAND_STAR_SOURCES_QUERY, {})

        # Count COLUMN_LINEAGE edges after expansion
        after = db.run_read(
            "MATCH ()-[r:COLUMN_LINEAGE {transform: 'STAR_EXPANSION'}]->() RETURN count(r) AS n",
            {},
        )
        after_count = after[0]["n"] if after else 0

        # Return the number of new edges created
        return max(0, after_count - before_count)

    def _ingest_external_consumers(self, db: GraphBackend, path: Path) -> dict:
        """Ingest declared external downstream consumers from .sqlcg.toml.

        Runs ONCE per index_repo call, after _expand_star_sources and before
        set_indexed_sha. Uses upsert_nodes_bulk/upsert_edges_bulk exclusively
        (never upsert_node/upsert_edge per-row — CLAUDE.md perf invariant).

        Returns:
            Dict with keys: consumers (int), edges (int), warnings (list[str])
        """
        specs = get_external_consumers(path)
        if not specs:
            return {"consumers": 0, "edges": 0, "warnings": []}

        presentation_prefixes = get_presentation_prefixes(path)

        # Gather all target table qualifieds across all specs for a single existence check
        all_targets: list[str] = []
        for spec in specs:
            all_targets.extend(spec.consumes)

        # Single UNWIND round-trip to check which targets exist as SqlTable nodes
        if all_targets:
            existing_rows = db.run_read(
                "UNWIND $names AS n MATCH (t:SqlTable {qualified: n}) RETURN n AS qualified",
                {"names": all_targets},
            )
            existing_tables: set[str] = {row["qualified"] for row in existing_rows}
        else:
            existing_tables = set()

        warnings: list[str] = []
        consumer_rows: list[dict] = []
        edge_rows: list[dict] = []

        for spec in specs:
            consumer_rows.append({"name": spec.name, "consumer_type": spec.consumer_type})
            for target in spec.consumes:
                if target not in existing_tables:
                    warnings.append(
                        f"Warning: external consumer '{spec.name}' "
                        f"references unknown table '{target}'"
                    )
                    continue
                # Check if this target is presentation-facing
                if presentation_prefixes and not any(
                    target.startswith(p) for p in presentation_prefixes
                ):
                    warnings.append(
                        f"Warning: external consumer '{spec.name}' "
                        f"references non-presentation table '{target}'"
                        f" — edge created anyway (advisory)"
                    )
                edge_rows.append({"src_key": target, "dst_key": spec.name})

        # Bulk upsert nodes, then edges — never per-row
        db.upsert_nodes_bulk(NodeLabel.EXTERNAL_CONSUMER, consumer_rows)
        if edge_rows:
            db.upsert_edges_bulk(
                NodeLabel.TABLE,
                NodeLabel.EXTERNAL_CONSUMER,
                RelType.CONSUMED_BY,
                edge_rows,
            )

        return {
            "consumers": len(consumer_rows),
            "edges": len(edge_rows),
            "warnings": warnings,
        }
