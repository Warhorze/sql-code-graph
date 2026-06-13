"""ANSI SQL parser implementation."""

import dataclasses
from pathlib import Path
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.base import ParsedFile, ParseQuality, QueryNode, SqlParser, TableRef
from sqlcg.parsers.registry import register
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


@register(None)  # None = ANSI/default dialect
class AnsiParser(SqlParser):
    """ANSI SQL parser for standard SQL dialects.

    Attributes:
        DIALECT: Set to None for ANSI/default dialect
    """

    DIALECT: str | None = None

    def __init__(
        self,
        schema_resolver: SchemaResolver,
        schema_aliases: dict[str, str] | None = None,
    ):
        """Initialize ANSI parser.

        Args:
            schema_resolver: SchemaResolver instance for table/column lookups
            schema_aliases: Optional table alias map (bare lowercase name → canonical name)
        """
        super().__init__(schema_resolver, schema_aliases=schema_aliases)

    @staticmethod
    def _compute_start_lines(sql: str) -> list[int]:
        """Compute 1-based start line for each semicolon-delimited statement.

        Uses the sqlglot tokenizer. Groups tokens by SEMICOLON boundaries and
        returns the .line of the first token in each group. O(tokens), called
        once per file before the statement loop.

        Args:
            sql: SQL text to tokenize

        Returns:
            List of 1-based line numbers, one per statement. May be shorter
            than the parsed statement count if the trailing statement lacks a
            semicolon — callers should guard with ``if stmt_index < len(lines)``.
        """
        from sqlglot.tokens import Tokenizer, TokenType  # type: ignore[attr-defined]

        tokens = Tokenizer().tokenize(sql)
        lines: list[int] = []
        group_start: int | None = None
        for tok in tokens:
            if group_start is None and tok.token_type != TokenType.SEMICOLON:
                group_start = tok.line
            elif tok.token_type == TokenType.SEMICOLON:
                if group_start is not None:
                    lines.append(group_start)
                group_start = None
        if group_start is not None:  # last statement with no trailing semicolon
            lines.append(group_start)
        return lines

    def parse_file(
        self,
        path: Path,
        sql: str,
        dependency_filter: set[str] | None = None,
        _precomputed_start_lines: list[int] | None = None,
        ddl_columns_by_bare: dict[str, list[str]] | None = None,
        rel_path: str | None = None,
    ) -> ParsedFile:
        """Parse SQL file and extract table/column lineage.

        Args:
            path: Path to the source file
            sql: SQL text to parse
            dependency_filter: optional set of lowercased table names. When provided,
                the cross-file sources seeded into `sources_map` are filtered to only those
                whose name is in the set. Pass-1 callers (and direct test callers) pass
                `None` to disable filtering; pass-2 callers
                (the `index_repo` pass-2 dispatch in `indexer.py`) compute this from
                the pass-1 `ParsedFile.referenced_tables`.
            _precomputed_start_lines: optional list of 1-based start lines, one per
                statement. When provided (e.g. by SnowflakeParser which computes the map
                from the preprocessed SQL after ``_preprocess_snowflake_sql`` — which
                preserves line count so preprocessed positions equal original-file
                positions), this list is used directly and ``_compute_start_lines`` is
                not called on the ``sql`` argument. When ``None``, ``_compute_start_lines
                (sql)`` is called here.
            ddl_columns_by_bare: optional bare-name → ordered DDL column list, filtered
                to the bare names this file's statements reference (mirrors the
                `dependency_filter`/`xfile_sql` filter discipline — payload stays
                O(N_refs), never O(N_corpus_tables)). Built by
                `CrossFileAggregator.register_pass1` (+ `resolve_clone_catalogs` for
                CLONE-inherited entries) and threaded through the pass-2 task dict.
                Used by the no-column-list positional-INSERT ordinal mapping (Part B,
                plan/sprints/positional_insert_clone_blindspot.md). `None` on the
                single-file / `reindex_file` path — Part B then degrades to the
                source-named + `inferred_from_source_name=True` fallback.

        Returns:
            ParsedFile with parsed statements and metadata
        """
        out = ParsedFile(path=path, dialect=self.DIALECT)

        # PR 3 (sprint_postmortem_fixes §PR 3 Step 3.1) — CTE/derived namespacing:
        # use the repo-relative posix path when available (threaded from index_repo
        # via the task dict) so graph keys are portable across machines and repos.
        # Falls back to str(path) for single-file / reindex_file callers that do not
        # supply rel_path; those callers should supply it for consistency.
        self._current_file_namespace = rel_path if rel_path is not None else str(path)
        # temp_table_namespacing (Step 1.1 / W4): reset alongside _current_file_namespace
        # so a re-used parser instance starts each file with a clean temp-key set.
        self._current_file_temp_keys = set()

        # Parse all statements in the file using the hook (allows subclass overrides)
        statements, parse_errors = self._do_parse(sql)
        out.errors.extend(parse_errors)

        # Apply dialect-specific statement-level transforms (e.g. USE SCHEMA qualification
        # in SnowflakeParser).  The base implementation is a no-op; subclasses override to
        # inject per-file context (e.g. current_schema tracking).  Must run BEFORE the
        # statement loop so that source/target table refs in subsequent statements are
        # already qualified when _parse_statement extracts them.
        # Perf invariant: no qualify/build_scope/expand/sg_lineage calls allowed here.
        statements = self._transform_statements(statements)

        if not statements:
            logger.debug("Failed to parse file %s", path)
            out.parse_quality = ParseQuality.FAILED
            return out

        # Compute 1-based start lines once per file, before the statement loop.
        # When _precomputed_start_lines is provided (Snowflake non-scripting path),
        # the caller has already computed the map from the preprocessed SQL.
        # _preprocess_snowflake_sql preserves line count (lambda replacements emit
        # the same number of newlines as the matched text), so preprocessed line
        # positions equal original-file positions — no line-shift desync.
        start_lines: list[int] = (
            _precomputed_start_lines
            if _precomputed_start_lines is not None
            else self._compute_start_lines(sql)
        )

        # Check for pure-DDL files (still parse table definitions but skip lineage)
        is_pure_ddl = self._is_pure_ddl_file(statements)
        if is_pure_ddl:
            out.errors.append("col_lineage_skip:pure_ddl_file")

        # Check for scripting fallback
        for stmt in statements:
            if stmt is not None and isinstance(stmt, exp.Command):
                out.parse_quality = ParseQuality.SCRIPTING_FALLBACK
                break

        # Build scope for all statements at once to avoid O(N) traversals
        from sqlglot.optimizer.scope import build_scope

        file_scopes: list[Any] = []
        for stmt in statements:
            try:
                file_scopes.append(build_scope(stmt) if stmt is not None else None)
            except Exception:
                file_scopes.append(None)

        # Compute as_dict once per file (not per statement)
        schema_dict = self._schema.as_dict() if self._schema else {}

        # Initialize sources_map to accumulate temp table definitions.
        # Seed with cross-file CTAS bodies from pass 1 (intra-file overrides).
        # When `dependency_filter` is provided (pass 2), keep only those CTAS bodies
        # the current file actually references — keeps exp.expand O(N_refs) instead
        # of O(N_corpus_ctas).
        if self._schema:
            xfile_sources_all = self._schema.cross_file_sources()
            if dependency_filter is not None:
                xfile_sources = {
                    name: body
                    for name, body in xfile_sources_all.items()
                    if name in dependency_filter
                }
            else:
                xfile_sources = dict(xfile_sources_all)
        else:
            xfile_sources = {}
        sources_map: dict[str, Any] = xfile_sources

        # Process each statement
        for stmt_index, stmt in enumerate(statements):
            if stmt is None:
                continue

            try:
                scope = file_scopes[stmt_index] if stmt_index < len(file_scopes) else None
                query_node = self._parse_statement(
                    stmt,
                    path,
                    stmt_index,
                    out,
                    sources_map,
                    scope=scope,
                    skip_column_lineage=is_pure_ddl,
                    schema_dict=schema_dict,
                    ddl_columns_by_bare=ddl_columns_by_bare,
                )
                # Assign 1-based start line from the pre-computed map (0 if out of range)
                query_node.start_line = (
                    start_lines[stmt_index] if stmt_index < len(start_lines) else 0
                )
                out.statements.append(query_node)

                # Register CTAS bodies in sources_map for downstream temp table references
                if isinstance(stmt, exp.Create):
                    # Unwrap Subquery wrapper: CREATE TABLE t AS (SELECT ...) has
                    # stmt.expression = exp.Subquery, not exp.Select directly
                    _expr = stmt.expression
                    if isinstance(_expr, exp.Subquery):
                        _expr = _expr.this
                    if isinstance(_expr, exp.Select) and query_node.target:
                        target_name = (query_node.target.name or "").lower()
                        if target_name:
                            sources_map[target_name] = _expr

                # Track defined and referenced tables
                if query_node.kind in ("CREATE_TABLE", "CREATE_VIEW"):
                    if query_node.target:
                        out.defined_tables.append(query_node.target)

                out.referenced_tables.extend(query_node.sources)

                # Upgrade to FULL if column lineage exists
                if query_node.column_lineage:
                    out.parse_quality = ParseQuality.FULL

            except Exception as exc:
                logger.warning("Failed to process statement %d in %s: %s", stmt_index, path, exc)
                out.errors.append(f"statement_error:{stmt_index}:{exc}")

        # PR 3 — clear namespace after parsing to avoid stale state on reuse
        self._current_file_namespace = None
        # temp_table_namespacing (Step 1.1 / W4): also clear the temp-key set at
        # file-end so a pool-reused parser instance carries no stale temp identity.
        self._current_file_temp_keys = set()
        return out

    def _do_parse(self, sql: str) -> tuple[list[Any], list[str]]:
        """Parse SQL text into statements, returning both statements and any parse errors.

        This method is a hook that can be overridden by subclasses (e.g., SnowflakeParser)
        to implement dialect-specific recovery or pre-processing logic.

        Args:
            sql: SQL text to parse

        Returns:
            Tuple of (statements list, error strings list). Errors are non-fatal;
            recovery may have collected partial statements even on parse failure.
        """
        try:
            return sqlglot.parse(sql, dialect=self.DIALECT), []
        except Exception as exc:
            return [], [f"parse_error:{exc}"]

    def _transform_statements(self, statements: list[Any]) -> list[Any]:
        """Hook for dialect-specific per-file statement transforms.

        Called once per file after ``_do_parse`` and before the statement loop in
        ``parse_file``.  The base implementation is a no-op that returns statements
        unchanged.  Subclasses override to inject per-file context such as USE SCHEMA
        qualification (SnowflakeParser).

        Perf invariant: implementations MUST NOT call qualify, build_scope, exp.expand,
        or sg_lineage — those are per-statement hot paths.  Only pure AST walks and
        mutations are permitted (O(N_statements × N_tables_per_stmt)).

        Args:
            statements: List of parsed sqlglot AST nodes (may include None entries).

        Returns:
            Transformed statements list (may be a new list or the same list mutated).
        """
        return statements

    def _parse_statement(
        self,
        stmt: Any,
        path: Path,
        stmt_index: int,
        out: ParsedFile,
        sources_map: dict[str, Any] | None = None,
        scope: Any = None,
        skip_column_lineage: bool = False,
        schema_dict: dict | None = None,
        ddl_columns_by_bare: dict[str, list[str]] | None = None,
    ) -> QueryNode:
        """Parse a single SQL statement into a QueryNode.

        Args:
            stmt: sqlglot AST node
            path: Path to the source file
            stmt_index: Statement index in the file
            out: ParsedFile object to append errors to
            sources_map: Map of temp table names to SELECT bodies for resolution
            scope: Pre-built sqlglot Scope for the statement (optional optimization)
            skip_column_lineage: When True, skip column lineage extraction (pure-DDL files)
            ddl_columns_by_bare: optional bare-name → ordered DDL column catalog index,
                threaded from `parse_file` for the no-column-list positional-INSERT
                ordinal mapping (Part B). See `parse_file` docstring for provenance.

        Returns:
            QueryNode with extracted metadata
        """
        kind = self._classify(stmt)
        sql = stmt.sql(dialect=self.DIALECT)

        # Extract target table for CREATE/INSERT statements
        target = None
        if isinstance(stmt, exp.Create):
            target = self._apply_table_alias(self._extract_target_table(stmt))
        elif isinstance(stmt, exp.Insert):
            target = self._apply_table_alias(self._extract_insert_target(stmt))

        # temp_table_namespacing Step 1.2 (Amendment 3): detect TEMPORARY property on a
        # CREATE target and stamp it role="temp" + namespace=current file.  Detection is
        # by AST property (exp.TemporaryProperty) — NEVER by name heuristic.  Runs once
        # per CREATE statement; zero per-column cost.  _extract_target_table is kept as a
        # pure static that returns the plain ref; role/namespace decoration happens here
        # where self and the Create AST are both available.
        # Guard: only when _current_file_namespace is set (i.e. parse_file is active) so
        # a bare TEMPORARY CREATE without a namespace stays role="table" and emits no
        # kind='temp' row — making the symmetric PK-collision scenario structurally
        # impossible (Amendment 6).
        if (
            isinstance(stmt, exp.Create)
            and stmt.find(exp.TemporaryProperty) is not None
            and target is not None
            and self._current_file_namespace is not None
        ):
            target = dataclasses.replace(
                target, role="temp", namespace=self._current_file_namespace
            )
            self._current_file_temp_keys.add(self._temp_identity(target.db, target.name))

        # View classification: a CREATE VIEW target is stamped role="view" so it lands
        # in the graph as SqlTable.kind='view' (indexer.py emits "kind": table.role).
        # Without this, views were indistinguishable from tables — the parser knew the
        # statement was a view (QueryKind.CREATE_VIEW on the SqlQuery node) but never
        # propagated it to the SqlTable node.  Downstream already expects 'view' as a
        # valid kind: indexer._USAGE_ELIGIBLE_KINDS = {'table','view'}, schema_resolver,
        # and the duckdb_backend kind-upgrade guard (kind IN ('table','view') is never
        # downgraded to 'derived').  Guarded on role == "table" so a TEMPORARY VIEW keeps
        # role="temp" (the temp block above runs first and wins).  Metadata-only: one
        # equality check per CREATE statement; zero per-column hot-path cost.
        if (
            isinstance(stmt, exp.Create)
            and stmt.kind == "VIEW"
            and target is not None
            and target.role == "table"
        ):
            target = dataclasses.replace(target, role="view")

        # C1 — capture the CLONE/LIKE source TableRef for:
        #   CREATE TABLE t CLONE src     → args['clone'] is exp.Clone
        #   CREATE TABLE t LIKE src      → args['properties'] contains exp.LikeProperty
        #
        # Both set clone_source so the existing resolve_clone_catalogs pass in the
        # indexer inherits src's DDL catalog columns for t automatically.
        #
        # Metadata-only: one args.get (CLONE) or one properties-list walk (LIKE) per
        # CREATE statement; no body/lineage impact (these statements have no SELECT body
        # — defined_body stays None, defined_columns stays empty).  Zero per-column
        # hot-path cost (plan/sprints/sprint_lineage_identity_and_session_context.md §PR 2,
        # Step 3.3; plan/sprints/positional_insert_clone_blindspot.md Part C1).
        #
        # Note on sqlglot dialect: in the Snowflake dialect, CREATE TABLE x LIKE y
        # parses LikeProperty into stmt.args['properties'], NOT into args['clone']
        # (verified during plan review 2026-06-11).  Standard ANSI also uses
        # LikeProperty for LIKE; CLONE is Snowflake-only and stays in args['clone'].
        clone_source: TableRef | None = None
        if isinstance(stmt, exp.Create) and stmt.kind == "TABLE":
            clone_arg = stmt.args.get("clone")
            if isinstance(clone_arg, exp.Clone) and isinstance(clone_arg.this, exp.Table):
                clone_source = self._apply_table_alias(
                    self._convert_table_expr_to_ref(clone_arg.this)
                )
            elif clone_source is None:
                # LIKE: look for LikeProperty in the properties list.
                props = stmt.args.get("properties")
                if props is not None:
                    for prop in getattr(props, "expressions", []):
                        if isinstance(prop, exp.LikeProperty) and isinstance(prop.this, exp.Table):
                            clone_source = self._apply_table_alias(
                                self._convert_table_expr_to_ref(prop.this)
                            )
                            break  # only one LIKE source is valid

        # Extract source tables
        sources = []
        ctes = {}
        confidence = 1.0
        parse_failed = False

        # PR5 (graph_health_catalog_and_metrics §7): a falsy/raising build_scope()
        # is only a *degraded extraction* for statement kinds that
        # _extract_column_lineage would otherwise have attempted (Select, INSERT
        # ... SELECT, CREATE ... AS SELECT). For everything else (Use, Set,
        # Comment, TruncateTable, Drop, Alter, Command, CREATE ... LIKE/CLONE/
        # column-defs, INSERT ... VALUES, Update, Delete, Merge), build_scope()
        # returning None is expected sqlglot behaviour for non-Select-rooted
        # statements and does NOT indicate lost lineage. Gating parse_failed on
        # this avoids 96% of the corpus's parse_failed=true rows being a pure
        # measurement artifact (96.1%/4,544 of 4,727 on the DWH corpus).
        can_have_lineage = self._can_have_column_lineage(stmt)

        # Try to extract table references using scope analysis
        try:
            # Use pre-built scope if provided, otherwise build it here (fallback)
            root_scope = scope
            if root_scope is None:
                from sqlglot.optimizer.scope import build_scope

                root_scope = build_scope(stmt)

            if root_scope:
                sources = self._real_tables(root_scope)
            else:
                # Fallback to basic table extraction
                sources = [
                    r
                    for s in self._fallback_table_scan(stmt)
                    if (r := self._apply_table_alias(s)) is not None
                ]
                parse_failed = can_have_lineage
        except Exception as exc:
            logger.debug("Failed to build scope for statement %d in %s: %s", stmt_index, path, exc)
            sources = [
                r
                for s in self._fallback_table_scan(stmt)
                if (r := self._apply_table_alias(s)) is not None
            ]
            parse_failed = can_have_lineage

        # Remove target from sources if present (CREATE/INSERT shouldn't select from target)
        if target:
            sources = [src for src in sources if src.full_id != target.full_id]

        # temp_table_namespacing Step 2.1 (Amendment 5 — exact placement): stamp any
        # source whose schema-qualified identity is registered in _current_file_temp_keys.
        # MUST be AFTER the target-exclusion filter (above) and BEFORE
        # _extract_column_lineage (below) so that the namespaced temp source flows into
        # the star-resolution path (query_sources=sources).  If stamping happens after
        # _extract_column_lineage, SELECT * FROM temp produces an orphaned bare-key
        # kind='table' star-source row.  O(1) set membership per source — zero extra
        # qualify/scope/expand cost.
        if self._current_file_temp_keys and self._current_file_namespace is not None:
            sources = [
                dataclasses.replace(s, role="temp", namespace=self._current_file_namespace)
                if self._temp_identity(s.db, s.name) in self._current_file_temp_keys
                else s
                for s in sources
            ]

        # Extract column lineage (skip for pure-DDL files)
        if skip_column_lineage:
            from sqlcg.parsers.base import LineageExtraction

            extraction = LineageExtraction(edges=[], star_sources=[])
        else:
            schema = (
                schema_dict
                if schema_dict is not None
                else (self._schema.as_dict() if self._schema else {})
            )
            extraction = self._extract_column_lineage(
                stmt,
                path,
                out,
                schema,
                dst_table=target,
                sources=sources_map,
                query_sources=sources,
                ddl_columns_by_bare=ddl_columns_by_bare,
            )
        column_lineage = extraction.edges
        star_sources = extraction.star_sources
        stmt_qualify_failed = extraction.qualify_failed

        # Extract defined columns for CREATE TABLE statements
        defined_columns: list[str] = []
        if isinstance(stmt, exp.Create) and stmt.kind == "TABLE":
            defined_columns = self._extract_defined_columns(stmt)

        # Extract defined body for CTAS statements (CREATE TABLE ... AS SELECT ...)
        defined_body: Any | None = None
        if isinstance(stmt, exp.Create):
            _expr = stmt.expression
            if isinstance(_expr, exp.Subquery):
                _expr = _expr.this
            if isinstance(_expr, exp.Select):
                defined_body = _expr

        # P3/P4: derive output columns from the SELECT projection for CREATE TABLE/VIEW AS SELECT.
        # Only when defined_columns is still empty (explicit column-list DDL already populated it).
        # Both TABLE and VIEW AS SELECT gain a catalog; CLONE/CREATE without SELECT body unchanged.
        if isinstance(stmt, exp.Create) and stmt.kind in ("TABLE", "VIEW") and not defined_columns:
            _body = stmt.expression
            if isinstance(_body, exp.Subquery):
                _body = _body.this
            if isinstance(_body, exp.Select):
                defined_columns = self._extract_select_output_columns(_body)

        # Remove duplicates while preserving order
        sources = self._deduplicate_table_refs(sources)

        return QueryNode(
            file=path,
            statement_index=stmt_index,
            sql=sql,
            kind=kind,
            target=target,
            sources=sources,
            ctes=ctes,
            column_lineage=column_lineage,
            parse_failed=parse_failed,
            qualify_failed=stmt_qualify_failed,
            confidence=confidence,
            parsing_mode="sqlglot",
            defined_columns=defined_columns,
            star_sources=star_sources,
            defined_body=defined_body,
            clone_source=clone_source,
        )

    @staticmethod
    def _extract_target_table(create_stmt: exp.Create) -> TableRef | None:
        """Extract the target table from a CREATE statement.

        Args:
            create_stmt: CREATE expression

        Returns:
            TableRef or None if not a table create
        """
        if create_stmt.kind not in ("TABLE", "VIEW"):
            return None

        if not create_stmt.this:
            return None

        return AnsiParser._convert_table_expr_to_ref(create_stmt.this)

    @staticmethod
    def _extract_insert_target(insert_stmt: exp.Insert) -> TableRef | None:
        """Extract the target table from an INSERT statement.

        Args:
            insert_stmt: INSERT expression

        Returns:
            TableRef or None
        """
        if not insert_stmt.this:
            return None

        return AnsiParser._convert_table_expr_to_ref(insert_stmt.this)

    @staticmethod
    def _extract_defined_columns(create_stmt: exp.Create) -> list[str]:
        """Extract column names from a CREATE TABLE statement.

        Args:
            create_stmt: CREATE expression (must be a TABLE create)

        Returns:
            List of column names in source order (as they appear in the SQL),
            normalized to lowercase with surrounding double-quotes stripped —
            identical to ColumnRef.__post_init__ — so DDL columns share node
            identity with lowercase ETL lineage columns (issue #38 Phase 5).
        """
        columns: list[str] = []
        # Find all ColumnDef nodes in the CREATE statement
        for col_def in create_stmt.find_all(exp.ColumnDef):
            col_name = col_def.this.name if col_def.this else None
            if col_name:
                if len(col_name) >= 2 and col_name[0] == '"' and col_name[-1] == '"':
                    col_name = col_name[1:-1]
                columns.append(col_name.lower())
        return columns

    @staticmethod
    def _extract_select_output_columns(select_body: exp.Select) -> list[str]:
        """Derive output column names from a SELECT projection list.

        For each projection: alias if present, else the bare column name. Star
        projections and unnamed/literal expressions yield nothing (graceful degrade).
        Names are lowercased + quote-stripped, identical to _extract_defined_columns /
        ColumnRef.__post_init__, so the catalog shares node identity with lineage cols.

        Used by P3 (CREATE VIEW AS SELECT) and P4 (CREATE TABLE AS SELECT) to populate
        defined_columns when no explicit column list is present.

        Args:
            select_body: A sqlglot exp.Select node (the outer SELECT of a CTAS or view)

        Returns:
            List of column names derived from the projection, in source order.
            Empty list if all projections are stars or unresolvable literals.
        """
        columns: list[str] = []
        for expr in select_body.expressions:
            # Alias wins: SELECT x AS col_a → col_a
            if expr.alias:
                col_name: str = expr.alias
            elif isinstance(expr, exp.Column):
                # Plain column ref: SELECT d.dn_datum → dn_datum; SELECT col → col
                col_name = expr.name
            elif isinstance(expr, (exp.Star,)):
                # SELECT * or SELECT t.* — cannot statically resolve; skip
                continue
            else:
                # Unnamed literal, function without alias, etc. — skip
                continue

            if not col_name or col_name == "*":
                continue

            # Normalize: lowercase + strip surrounding double-quotes
            if len(col_name) >= 2 and col_name[0] == '"' and col_name[-1] == '"':
                col_name = col_name[1:-1]
            columns.append(col_name.lower())
        return columns

    @staticmethod
    def _fallback_table_scan(stmt: Any) -> list[TableRef]:
        """Fallback table extraction when scope analysis fails.

        Walks the AST tree to find all Table nodes.

        Args:
            stmt: SQL expression

        Returns:
            List of TableRef objects found
        """
        tables = []
        seen = set()

        # Use find_all to iterate through all nodes of type Table
        for table_node in stmt.find_all(exp.Table):
            ref = AnsiParser._convert_table_expr_to_ref(table_node)
            if ref and ref.full_id not in seen:
                tables.append(ref)
                seen.add(ref.full_id)

        return tables

    @staticmethod
    def _deduplicate_table_refs(tables: list[TableRef]) -> list[TableRef]:
        """Remove duplicate table references while preserving order.

        Args:
            tables: List of TableRef objects

        Returns:
            Deduplicated list
        """
        seen = set()
        result = []
        for table in tables:
            if table.full_id not in seen:
                result.append(table)
                seen.add(table.full_id)
        return result
