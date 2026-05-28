"""Main indexer orchestrating parsing and graph persistence."""

import multiprocessing as mp
import queue
from collections.abc import Callable
from pathlib import Path

from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.queries import STALE_VIEWS_QUERY
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.indexer.error_classify import _classify_error
from sqlcg.indexer.pool import HardKillPool
from sqlcg.indexer.walker import walk_sql_files
from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.base import ParsedFile
from sqlcg.parsers.registry import get_parser
from sqlcg.utils.ignore import load_ignore_spec
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


def _subprocess_parse_worker(parser_cls, dialect, path, sql, q):
    """Parse a single file in a subprocess; queue the ParsedFile (or exception).

    parser_cls must be the *class* (pickleable), not an instance. The worker
    instantiates the parser inside the child process so that any thread-local
    state from the parent does not leak.

    T-09-04: Parser constructors require a SchemaResolver.
    The subprocess has no schema loaded (no CSV in the child), so pass a
    fresh empty resolver. The qualify-once path in the child still calls
    self._schema.mapping_schema() which returns {} on an empty resolver,
    giving the same infer-only behaviour as small-repo mode. This is correct
    because schema context is passed via mapping_schema at the parse_file call
    site, not via the resolver state when subprocess isolation is active.
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
        timeout_per_file: int = 5,
        use_git: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
        schema_csv: Path | None = None,
        no_ddl: bool = False,
        batch_size: int = 50,
        n_workers: int | None = None,
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
            schema_csv: Explicit path to INFORMATION_SCHEMA.COLUMNS CSV. When None,
                auto-discovers <path>/.sqlcg/schema.csv if present.
            no_ddl: When True, skip DDL-only files from upsert into the graph
            batch_size: Number of files committed per KuzuDB transaction in the upsert pass.
                Higher values reduce commit overhead but increase the per-batch recovery cost
                and the working-set size when a transaction holds locks. Default 50 is a
                balance between throughput on large corpora (1,000+ files) and memory/lock
                pressure on small ones. batch_size=1 reproduces the legacy per-file commit
                behaviour. Must be >= 1.
            n_workers: Number of worker processes in the persistent pool (default: cpu_count()).
                Workers are spawn-mode so they never inherit the KuzuDB file descriptor.
                Each worker holds two pre-built parsers (pass-1 empty schema, pass-2 full
                schema CSV), paying the cold-start cost once per run rather than per file.

        Returns:
            Dict with keys: files_parsed, parse_errors, tables_found,
            lineage_edges_created, quality, batch_size
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        # Load explicit INFORMATION_SCHEMA CSV if provided (into graph DB only;
        # workers receive the CSV path directly for their own schema resolvers)
        if schema_csv and schema_csv.exists():
            from sqlcg.cli.commands.load_schema import _load_schema_into_graph

            _load_schema_into_graph(schema_csv, include_catalog=False, db=db)

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

        schema_csv_str = str(schema_csv) if schema_csv and schema_csv.exists() else None
        p1_timeout = float(timeout_per_file) if timeout_per_file > 0 else float("inf")
        p2_timeout = max(p1_timeout, 15.0) if timeout_per_file > 0 else float("inf")

        p1_tasks = [{"type": "parse_pass1", "path": str(fp), "sql": sql} for fp, sql in file_sqls]

        try:
            with HardKillPool(dialect, schema_csv_str, n_workers) as pool:
                # Pass 1: parse all files in parallel
                p1_raw = pool.map(p1_tasks, per_task_timeout=p1_timeout)

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

                    if progress_callback is not None and (i + 1) % 100 == 0:
                        progress_callback(i + 1, total_files)

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

                # Pass 2: resolve cross-file references in parallel
                p2_raw = pool.map(p2_tasks, per_task_timeout=p2_timeout)

        except KeyboardInterrupt:
            logger.info("SIGINT received — flushing pass-1 progress")
            # pass1_results may be partial; upsert what we have
            self._upsert_all(pass1_results, db)
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

        # Load gold tables (information_schema-backed tables) before upserting
        # to suppress DDL columns for tables with production-verified schemas
        gold_rows = db.run_read(
            "MATCH (t:SqlTable)-[r:HAS_COLUMN {source: 'information_schema'}]->() "
            "RETURN DISTINCT t.qualified AS q",
            {},
        )
        gold_tables: frozenset[str] = frozenset(row["q"] for row in gold_rows)

        # Upsert all results and count quality distribution using batched transactions
        nonlocal_counts: dict = {
            "tables": 0,
            "edges": 0,
            "star_sources": 0,
            "columns_defined": 0,
            "quality": {"full": 0, "table_only": 0, "scripting_fallback": 0, "failed": 0},
        }

        def _flush_batch(batch: list[ParsedFile]) -> None:
            """Upsert a batch of ParsedFile objects in a single transaction.

            On any per-file exception, the file is recorded as failed but the
            transaction continues for the remaining files in the batch. This
            matches the legacy per-file behaviour where one bad file did not
            abort the whole index run.

            Note: sqlcg watch's reindex_file uses a separate code path with
            its own short per-file transaction. PERF-BATCH only affects index_repo.
            """
            if not batch:
                return
            with db.transaction():
                for parsed_in_batch in batch:
                    try:
                        counts = self._upsert_parsed_file(
                            parsed_in_batch, db, gold_tables=gold_tables
                        )
                        nonlocal_counts["tables"] += counts["tables"]
                        nonlocal_counts["edges"] += counts["edges"]
                        nonlocal_counts["star_sources"] += counts.get("star_sources", 0)
                        nonlocal_counts["columns_defined"] += counts.get("columns_defined", 0)
                        q_key = parsed_in_batch.parse_quality.value.lower()
                        nonlocal_counts["quality"][q_key] += 1
                    except Exception as exc:
                        logger.warning(
                            "Failed to upsert %s: %s — skipping", parsed_in_batch.path, exc
                        )
                        nonlocal_counts["quality"]["failed"] += 1

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

        # Post-ingestion: expand STAR_SOURCE edges into concrete COLUMN_LINEAGE edges
        star_edges_expanded = self._expand_star_sources(db)

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

        return {
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
        }

    def reindex_file(self, file_path: str, db: GraphBackend, dialect: str | None) -> None:
        """Re-index a single file and its dependent views.

        Args:
            file_path: Path to the file to re-index
            db: GraphBackend instance
            dialect: SQL dialect (None for ANSI)
        """
        stale_views = db.run_read(STALE_VIEWS_QUERY, {"path": file_path})

        with db.transaction():
            db.delete_nodes_for_file(file_path)
            schema_resolver = SchemaResolver(dialect=dialect)
            parser = get_parser(dialect, schema_resolver)
            sql = Path(file_path).read_text(encoding="utf-8")
            parsed = parser.parse_file(Path(file_path), sql)

            # Load gold tables for this re-index call
            gold_rows = db.run_read(
                "MATCH (t:SqlTable)-[r:HAS_COLUMN {source: 'information_schema'}]->() "
                "RETURN DISTINCT t.qualified AS q",
                {},
            )
            gold_tables = frozenset(row["q"] for row in gold_rows)

            self._upsert_parsed_file(parsed, db, gold_tables=gold_tables)

        for row in stale_views:
            self._reindex_view_definition(row["view_name"], db, dialect)

        # Re-run star expansion after re-indexing and all stale views are processed
        # Call this outside the transaction
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

    def _upsert_parsed_file(
        self,
        parsed: ParsedFile,
        db: GraphBackend,
        gold_tables: frozenset[str] = frozenset(),
    ) -> dict:
        """Map ParsedFile → graph nodes/edges using bulk upsert.

        Args:
            parsed: ParsedFile to upsert
            db: GraphBackend instance
            gold_tables: Set of table full_ids with information_schema-backed columns
                        (skip DDL column writes for these tables)

        Returns:
            Dict with keys: tables, edges, columns_defined, star_sources
        """
        counts = {"tables": 0, "edges": 0, "columns_defined": 0, "star_sources": 0}

        # --- Phase A: collect rows from parsed model ---

        file_rows: list[dict] = []
        table_rows: list[dict] = []  # NodeLabel.TABLE
        column_rows: list[dict] = []  # NodeLabel.COLUMN
        query_rows: list[dict] = []  # NodeLabel.QUERY

        defined_in_edges: list[dict] = []  # TABLE -> FILE
        has_column_edges: list[dict] = []  # TABLE -> COLUMN
        query_defined_in_edges: list[dict] = []  # QUERY -> FILE
        selects_from_edges: list[dict] = []  # QUERY -> TABLE
        column_lineage_edges: list[dict] = []  # COLUMN -> COLUMN
        star_source_edges: list[dict] = []  # QUERY -> TABLE

        # File node
        file_rows.append({"path": parsed.path_str, "dialect": parsed.dialect or ""})

        # Defined tables
        for table in parsed.defined_tables:
            # Guard: detect duplicate DDL for the same table across files
            existing = db.run_read(
                f"MATCH (t:{NodeLabel.TABLE} {{qualified: $qid}}) RETURN t.defined_in_file AS f",
                {"qid": table.full_id},
            )
            if existing and existing[0]["f"] and existing[0]["f"] != parsed.path_str:
                logger.warning(
                    "Table %s already defined in %s — %s will add columns to the union; "
                    "star expansion may include columns from the earlier DDL file",
                    table.full_id,
                    existing[0]["f"],
                    parsed.path_str,
                )
                if hasattr(parsed, "errors"):
                    parsed.errors.append(
                        f"duplicate_ddl:{table.full_id}:already_in:{existing[0]['f']}"
                    )

            table_rows.append(
                {
                    "qualified": table.full_id,
                    "name": table.name,
                    "catalog": table.catalog or "",
                    "db": table.db or "",
                    "kind": "TABLE",
                    "defined_in_file": parsed.path_str,
                }
            )
            defined_in_edges.append({"src_key": table.full_id, "dst_key": parsed.path_str})
            counts["tables"] += 1

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
            # Check if this table is covered by information_schema (gold table)
            if table.full_id in gold_tables:
                logger.debug(
                    "Skipping DDL columns for %s — information_schema takes precedence",
                    table.full_id,
                )
                continue

            qnode = defined_by_query.get(table.full_id)
            if not qnode:
                continue

            for col_name in qnode.defined_columns:
                col_id = f"{table.full_id}.{col_name}"
                column_rows.append(
                    {
                        "id": col_id,
                        "col_name": col_name,
                        "table_qualified": table.full_id,
                        "catalog": table.catalog or "",
                        "db": table.db or "",
                        "table_name": table.name,
                    }
                )
                has_column_edges.append(
                    {"src_key": table.full_id, "dst_key": col_id, "source": "ddl"}
                )
                counts["columns_defined"] += 1

        # Query nodes and related edges
        for i, stmt in enumerate(parsed.statements):
            query_id = f"{parsed.path_str}:{i}"
            query_rows.append(
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
                }
            )
            query_defined_in_edges.append({"src_key": query_id, "dst_key": parsed.path_str})

            # Source table edges
            for src_table in stmt.sources:
                table_rows.append(
                    {
                        "qualified": src_table.full_id,
                        "name": src_table.name,
                        "catalog": src_table.catalog or "",
                        "db": src_table.db or "",
                        "kind": "TABLE",
                        "defined_in_file": "",
                    }
                )
                selects_from_edges.append({"src_key": query_id, "dst_key": src_table.full_id})

            # Column lineage edges
            for edge in stmt.column_lineage:
                src_id = edge.src.full_id
                dst_id = edge.dst.full_id
                column_rows.append(
                    {
                        "id": src_id,
                        "col_name": edge.src.name,
                        "table_qualified": edge.src.table.full_id,
                        "catalog": edge.src.table.catalog or "",
                        "db": edge.src.table.db or "",
                        "table_name": edge.src.table.name,
                    }
                )
                column_rows.append(
                    {
                        "id": dst_id,
                        "col_name": edge.dst.name,
                        "table_qualified": edge.dst.table.full_id,
                        "catalog": edge.dst.table.catalog or "",
                        "db": edge.dst.table.db or "",
                        "table_name": edge.dst.table.name,
                    }
                )
                column_lineage_edges.append(
                    {
                        "src_key": src_id,
                        "dst_key": dst_id,
                        "transform": edge.transform,
                        "confidence": edge.confidence,
                        "query_id": query_id,
                    }
                )
                counts["edges"] += 1

            # STAR_SOURCE edges for graph-backend expansion
            for star in stmt.star_sources:
                table_rows.append(
                    {
                        "qualified": star.source.full_id,
                        "name": star.source.name,
                        "catalog": star.source.catalog or "",
                        "db": star.source.db or "",
                        "kind": "TABLE",
                        "defined_in_file": "",
                    }
                )
                star_source_edges.append(
                    {
                        "src_key": query_id,
                        "dst_key": star.source.full_id,
                        "qualifier": star.qualifier or "<unqualified>",
                        "target_table": stmt.target.full_id if stmt.target else "",
                        "confidence": 0.8,
                    }
                )
                counts["star_sources"] += 1

            # Upsert target table node (if not already a defined_table)
            # so that star expansion can create destination columns
            if stmt.target and stmt.target.full_id not in defined_table_ids:
                table_rows.append(
                    {
                        "qualified": stmt.target.full_id,
                        "name": stmt.target.name,
                        "catalog": stmt.target.catalog or "",
                        "db": stmt.target.db or "",
                        "kind": "TABLE",
                        "defined_in_file": "",
                    }
                )

        # --- Phase B: deduplicate rows within each group ---
        # Cypher MERGE is idempotent under deduplication, but we deduplicate in Python
        # to reduce payload size
        table_rows = list({r["qualified"]: r for r in table_rows}.values())
        column_rows = list({r["id"]: r for r in column_rows}.values())
        query_rows = list({r["id"]: r for r in query_rows}.values())

        # --- Phase C: flush in dependency order (nodes before their edges) ---
        db.upsert_nodes_bulk(NodeLabel.FILE, file_rows)
        db.upsert_nodes_bulk(NodeLabel.TABLE, table_rows)
        db.upsert_nodes_bulk(NodeLabel.COLUMN, column_rows)
        db.upsert_nodes_bulk(NodeLabel.QUERY, query_rows)

        db.upsert_edges_bulk(NodeLabel.TABLE, NodeLabel.FILE, RelType.DEFINED_IN, defined_in_edges)
        db.upsert_edges_bulk(
            NodeLabel.TABLE, NodeLabel.COLUMN, RelType.HAS_COLUMN, has_column_edges
        )
        db.upsert_edges_bulk(
            NodeLabel.QUERY, NodeLabel.FILE, RelType.QUERY_DEFINED_IN, query_defined_in_edges
        )
        db.upsert_edges_bulk(
            NodeLabel.QUERY, NodeLabel.TABLE, RelType.SELECTS_FROM, selects_from_edges
        )
        db.upsert_edges_bulk(
            NodeLabel.COLUMN, NodeLabel.COLUMN, RelType.COLUMN_LINEAGE, column_lineage_edges
        )
        db.upsert_edges_bulk(
            NodeLabel.QUERY, NodeLabel.TABLE, RelType.STAR_SOURCE, star_source_edges
        )

        return counts

    def _upsert_all(self, results: list[ParsedFile], db: GraphBackend) -> None:
        """Upsert all parsed files.

        Args:
            results: List of ParsedFile objects
            db: GraphBackend instance
        """
        for parsed in results:
            self._upsert_parsed_file(parsed, db)

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

    def _reindex_view_definition(
        self, view_name: str, db: GraphBackend, dialect: str | None
    ) -> None:
        """Re-index the file that defines a view.

        Args:
            view_name: Qualified view name
            db: GraphBackend instance
            dialect: SQL dialect
        """
        query = (
            f"MATCH (t:{NodeLabel.TABLE} {{qualified: $name}})"
            f"-[:{RelType.DEFINED_IN}]->(f:{NodeLabel.FILE}) "
            "RETURN f.path AS path"
        )
        result = db.run_read(query, {"name": view_name})
        for row in result:
            self.reindex_file(row["path"], db, dialect)
