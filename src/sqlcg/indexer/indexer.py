"""Main indexer orchestrating parsing and graph persistence."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from sqlcg.core.graph_db import GraphBackend
from sqlcg.core.queries import STALE_VIEWS_QUERY
from sqlcg.core.schema import NodeLabel, RelType
from sqlcg.indexer.walker import walk_sql_files
from sqlcg.lineage.aggregator import CrossFileAggregator
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.base import ParsedFile
from sqlcg.parsers.registry import get_parser
from sqlcg.utils.ignore import load_ignore_spec
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


class Indexer:
    """Orchestrates SQL file parsing and graph persistence."""

    def index_repo(
        self,
        path: Path,
        dialect: str | None,
        db: GraphBackend,
        dbt_manifest: Path | None = None,
        timeout_per_file: int = 30,
        use_git: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
        schema_csv: Path | None = None,
        no_ddl: bool = False,
        batch_size: int = 50,
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

        Returns:
            Dict with keys: files_parsed, parse_errors, tables_found,
            lineage_edges_created, quality, batch_size
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        # Load explicit INFORMATION_SCHEMA CSV if provided
        if schema_csv and schema_csv.exists():
            from sqlcg.cli.commands.load_schema import _load_schema_into_graph

            _load_schema_into_graph(schema_csv, include_catalog=False, db=db)

        spec = load_ignore_spec(path)
        schema_resolver = SchemaResolver(dialect=dialect)
        parser = get_parser(dialect, schema_resolver)
        aggregator = CrossFileAggregator()

        files = list(walk_sql_files(path, spec, use_git=use_git))
        pass1_results: list[ParsedFile] = []
        parse_errors = 0
        total_files = len(files)

        # Pass 1: parse all files
        for i, file_path in enumerate(files, 1):
            try:
                sql = file_path.read_text(encoding="utf-8")
                parsed = self._index_single_file(parser, file_path, sql, timeout_per_file)
                aggregator.register_pass1(parsed)
                pass1_results.append(parsed)
                parse_errors += len(parsed.errors)
            except KeyboardInterrupt:
                logger.info("SIGINT received — flushing progress")
                self._upsert_all(pass1_results, db)
                raise
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", file_path, exc)
                parse_errors += 1

            # Invoke progress callback every 100 files
            if progress_callback is not None and i % 100 == 0:
                progress_callback(i, total_files)

        # Optional: load dbt manifest
        if dbt_manifest:
            from sqlcg.indexer.dbt_adapter import load_dbt_manifest

            load_dbt_manifest(dbt_manifest, schema_resolver)

        # Seed cross-file sources for pass-2 re-parsing
        schema_resolver.register_cross_file_sources(aggregator.cross_file_sources)

        # Pass 2: resolve cross-file references
        pass2_results: list[ParsedFile] = []
        for parsed in pass1_results:
            try:
                resolved = aggregator.resolve_pass2(parser, parsed)
                pass2_results.append(resolved)
            except Exception as exc:
                logger.warning("resolve_pass2 failed for %s: %s", parsed.path, exc)
                pass2_results.append(parsed)

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

        return {
            "files_parsed": len(pass2_results),
            "parse_errors": parse_errors,
            "tables_found": nonlocal_counts["tables"],
            "lineage_edges_created": nonlocal_counts["edges"],
            "columns_defined": nonlocal_counts["columns_defined"],
            "star_sources": nonlocal_counts["star_sources"],
            "star_edges_expanded": star_edges_expanded,
            "quality": nonlocal_counts["quality"],
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
        """Parse one file, with optional timeout.

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

        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(parser.parse_file, path, sql)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeout:
                logger.warning("Timeout parsing %s (>%ds) — skipping", path, timeout)
                out = ParsedFile(path=path, dialect=parser.DIALECT)
                out.errors.append(f"timeout:{timeout}s")
                return out

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
        """Map ParsedFile → graph nodes/edges.

        Args:
            parsed: ParsedFile to upsert
            db: GraphBackend instance
            gold_tables: Set of table full_ids with information_schema-backed columns
                        (skip DDL column writes for these tables)

        Returns:
            Dict with keys: tables, edges, columns_defined
        """
        counts = {"tables": 0, "edges": 0, "columns_defined": 0}

        # Upsert File node
        db.upsert_node(
            NodeLabel.FILE,
            parsed.path_str,
            {
                "path": parsed.path_str,
                "dialect": parsed.dialect or "",
            },
        )

        # Upsert defined tables (with duplicate DDL detection)
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

            db.upsert_node(
                NodeLabel.TABLE,
                table.full_id,
                {
                    "qualified": table.full_id,
                    "name": table.name,
                    "catalog": table.catalog or "",
                    "db": table.db or "",
                    "kind": "TABLE",
                    "defined_in_file": parsed.path_str,
                },
            )
            db.upsert_edge(
                NodeLabel.TABLE,
                table.full_id,
                NodeLabel.FILE,
                parsed.path_str,
                RelType.DEFINED_IN,
                {},
            )
            counts["tables"] += 1

        # Build a map of target.full_id -> QueryNode for column lookup
        defined_by_query = {
            s.target.full_id: s
            for s in parsed.statements
            if s.target and s.kind in ("CREATE_TABLE", "CREATE_VIEW") and s.defined_columns
        }

        # Compute set of defined table IDs for quick lookup
        defined_table_ids = {t.full_id for t in parsed.defined_tables}

        # Upsert DDL columns and HAS_COLUMN edges
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
                db.upsert_node(
                    NodeLabel.COLUMN,
                    col_id,
                    {
                        "id": col_id,
                        "col_name": col_name,
                        "table_qualified": table.full_id,
                        "catalog": table.catalog or "",
                        "db": table.db or "",
                        "table_name": table.name,
                    },
                )
                db.upsert_edge(
                    NodeLabel.TABLE,
                    table.full_id,
                    NodeLabel.COLUMN,
                    col_id,
                    RelType.HAS_COLUMN,
                    {"source": "ddl"},
                )
                counts["columns_defined"] += 1

        # Upsert query nodes
        for i, stmt in enumerate(parsed.statements):
            query_id = f"{parsed.path_str}:{i}"
            db.upsert_node(
                NodeLabel.QUERY,
                query_id,
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
                },
            )
            db.upsert_edge(
                NodeLabel.QUERY,
                query_id,
                NodeLabel.FILE,
                parsed.path_str,
                RelType.QUERY_DEFINED_IN,
                {},
            )

            # Source table edges
            for src_table in stmt.sources:
                db.upsert_node(
                    NodeLabel.TABLE,
                    src_table.full_id,
                    {
                        "qualified": src_table.full_id,
                        "name": src_table.name,
                        "catalog": src_table.catalog or "",
                        "db": src_table.db or "",
                        "kind": "TABLE",
                        "defined_in_file": "",
                    },
                )
                db.upsert_edge(
                    NodeLabel.QUERY,
                    query_id,
                    NodeLabel.TABLE,
                    src_table.full_id,
                    RelType.SELECTS_FROM,
                    {},
                )

            # Column lineage edges
            for edge in stmt.column_lineage:
                src_id = edge.src.full_id
                dst_id = edge.dst.full_id
                db.upsert_node(
                    NodeLabel.COLUMN,
                    src_id,
                    {
                        "id": src_id,
                        "col_name": edge.src.name,
                        "table_qualified": edge.src.table.full_id,
                        "catalog": edge.src.table.catalog or "",
                        "db": edge.src.table.db or "",
                        "table_name": edge.src.table.name,
                    },
                )
                db.upsert_node(
                    NodeLabel.COLUMN,
                    dst_id,
                    {
                        "id": dst_id,
                        "col_name": edge.dst.name,
                        "table_qualified": edge.dst.table.full_id,
                        "catalog": edge.dst.table.catalog or "",
                        "db": edge.dst.table.db or "",
                        "table_name": edge.dst.table.name,
                    },
                )
                db.upsert_edge(
                    NodeLabel.COLUMN,
                    src_id,
                    NodeLabel.COLUMN,
                    dst_id,
                    RelType.COLUMN_LINEAGE,
                    {
                        "transform": edge.transform,
                        "confidence": edge.confidence,
                        "query_id": query_id,
                    },
                )
                counts["edges"] += 1

            # STAR_SOURCE edges for graph-backend expansion
            for star in stmt.star_sources:
                db.upsert_node(
                    NodeLabel.TABLE,
                    star.source.full_id,
                    {
                        "qualified": star.source.full_id,
                        "name": star.source.name,
                        "catalog": star.source.catalog or "",
                        "db": star.source.db or "",
                        "kind": "TABLE",
                        "defined_in_file": "",
                    },
                )
                db.upsert_edge(
                    NodeLabel.QUERY,
                    query_id,
                    NodeLabel.TABLE,
                    star.source.full_id,
                    RelType.STAR_SOURCE,
                    {
                        "qualifier": star.qualifier or "<unqualified>",
                        "target_table": stmt.target.full_id if stmt.target else "",
                        "confidence": 0.8,
                    },
                )
                counts["star_sources"] = counts.get("star_sources", 0) + 1

            # Upsert target table node (if not already a defined_table)
            # so that star expansion can create destination columns
            if stmt.target and stmt.target.full_id not in defined_table_ids:
                db.upsert_node(
                    NodeLabel.TABLE,
                    stmt.target.full_id,
                    {
                        "qualified": stmt.target.full_id,
                        "name": stmt.target.name,
                        "catalog": stmt.target.catalog or "",
                        "db": stmt.target.db or "",
                        "kind": "TABLE",
                        "defined_in_file": "",
                    },
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
