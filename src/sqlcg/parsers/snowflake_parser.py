"""Snowflake SQL parser with scripting block detection and DML extraction."""

import re
from pathlib import Path
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import ParsedFile, ParseQuality
from sqlcg.parsers.registry import register
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

# Regex for detecting scripting blocks (BEGIN/IF/LOOP)
# Used as fallback when tokenization fails
_SCRIPTING_BLOCK = re.compile(r"\bBEGIN\b", re.IGNORECASE)

# Regex for extracting DML statements from scripting blocks.
# Does not handle ';' inside string literals — tokenizer-based extraction deferred to v2.
_EMBEDDED_DML = re.compile(
    r"(SELECT\s+.+?(?=;|\Z)|INSERT\s+INTO.+?(?=;|\Z)|UPDATE\s+.+?(?=;|\Z)|DELETE\s+.+?(?=;|\Z)|MERGE\s+INTO.+?(?=;|\Z))",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)

# Regex for scanning USE SCHEMA / USE db.schema statements in scripting blocks
# by raw text position.  Captures the schema name (last dot-segment) so we can
# determine which schema was active at each DML match offset.
#
# Matches (schema = group 1 or group 2):
#   USE SCHEMA <schema>;
#   USE SCHEMA <db>.<schema>;
#   USE <db>.<schema>;
#
# Excludes (no schema derivable):
#   USE DATABASE <db>;  — multi-word form never matches (SCHEMA keyword or a
#                         dotted two-part name is required)
#   USE <bare_name>;    — Snowflake semantics: a one-part USE sets the DATABASE,
#                         not the schema; qualifying with it would put a database
#                         name in the schema slot of graph keys (plan W3)
#
# False-positive risk: a USE statement inside a string literal or comment will be
# captured.  This is acceptable — it is bounded (at most one spurious schema per
# occurrence, and real USE statements are common while USE-in-strings is rare in
# ETL code).
_USE_SCHEMA_RE = re.compile(
    r"\bUSE\s+(?:SCHEMA\s+(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)|[A-Za-z_]\w*\.([A-Za-z_]\w*))\s*;",
    re.IGNORECASE,
)

# PR-6 (dynamic-SQL EXECUTE IMMEDIATE unwrap, Phase 1).
#
# Snowflake holds dynamic SQL either inline as a literal or in a STRING variable
# that is later executed.  We recover ONLY the statically-knowable case: a single
# string literal (possibly split by `|| <runtime> ||` concatenation OUTSIDE the
# AS SELECT body — e.g. a WAREHOUSE clause).  Concatenation INSIDE the body, bind
# variables (`:var`), and CONCAT(...) are runtime-dependent → NOT recoverable and
# are skipped (see _unwrap_execute_immediate).
#
# Matches the two execution surfaces:
#   EXECUTE IMMEDIATE (:var)    /  EXECUTE IMMEDIATE :var   → resolve `var := '...'`
#   EXECUTE IMMEDIATE '<lit>'                                → inline literal
_EXEC_IMMEDIATE_VAR_RE = re.compile(
    r"EXECUTE\s+IMMEDIATE\s*\(?\s*:([A-Za-z_]\w*)\s*\)?",
    re.IGNORECASE,
)
_EXEC_IMMEDIATE_LITERAL_RE = re.compile(
    r"EXECUTE\s+IMMEDIATE\s*\(?\s*(?=')",
    re.IGNORECASE,
)

# Placeholder substituted for a runtime `|| <fragment> ||` concatenation that sits
# OUTSIDE the AS SELECT body (only the Snowflake WAREHOUSE clause in the corpus).
# Lossless for lineage: it never appears inside a FROM/JOIN/SELECT clause.
_DYNAMIC_RUNTIME_PLACEHOLDER = "__SQLCG_DYNAMIC_RUNTIME__"

# Marks the start of a CREATE [...] AS <body>.  A runtime concatenation placeholder
# at/after this point means the body itself is dynamic → the file is non-recoverable.
_AS_BODY_START_RE = re.compile(r"\bAS\b\s*(?:WITH\b|SELECT\b|\()", re.IGNORECASE)

# In-scope DDL prefix: only a CREATE [OR REPLACE] [TEMP|TEMPORARY|TRANSIENT|DYNAMIC|
# ...] TABLE|VIEW is statically recoverable.  SHOW / DROP / bare SELECT / INSERT /
# UPDATE / MERGE / CALL recovered from an EXECUTE IMMEDIATE are out of scope (the plan
# limits Phase 1 to static-literal CREATE … AS SELECT DDL).
_CREATE_DDL_PREFIX_RE = re.compile(
    r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:\w+\s+)*?(?:TABLE|VIEW)\b",
    re.IGNORECASE,
)


@register("snowflake")
class SnowflakeParser(AnsiParser):
    """Snowflake SQL parser with scripting block handling.

    Handles Snowflake-specific features:
    - Token-aware scripting block detection (avoids false-positives)
    - DML extraction from scripting blocks
    - Colon-qualified identifiers (Gap 1)
    - LATERAL FLATTEN operations (Gap 2)
    - Dynamic identifiers (Gap 3)
    - WITH TAG clause stripping (Gap 4)
    - ALTER ... SET TAG statement stripping (Gap 4b)
    """

    DIALECT: str | None = "snowflake"

    def __init__(
        self,
        schema_resolver: SchemaResolver,
        schema_aliases: dict[str, str] | None = None,
    ):
        """Initialize Snowflake parser.

        Args:
            schema_resolver: SchemaResolver instance for table/column lookups
            schema_aliases: Optional table alias map (bare lowercase name → canonical name)
        """
        super().__init__(schema_resolver, schema_aliases=schema_aliases)

    def parse_file(
        self,
        path: Path,
        sql: str,
        dependency_filter: set[str] | None = None,
        ddl_columns_by_bare: dict[str, list[str]] | None = None,
        rel_path: str | None = None,
    ) -> ParsedFile:
        """Parse Snowflake SQL file with scripting block detection.

        Args:
            path: Path to the source file
            sql: SQL text to parse
            dependency_filter: optional set of lowercased table names to filter cross-file sources
                (passed to AnsiParser.parse_file; see that method for full documentation)
            ddl_columns_by_bare: optional bare-name → ordered DDL column list, filtered
                and forwarded to AnsiParser.parse_file (see that method for full documentation)
            rel_path: repo-relative posix path string (e.g. "etl/sql/fact/wtfa.sql") used to
                key CTE/derived namespaces portably.  Threaded from index_repo via the task dict.
                Falls back to str(path) when None (single-file / reindex_file callers).
                See sprint_postmortem_fixes.md §PR 3 Step 3.1.

        Returns:
            ParsedFile with parsed statements and metadata
        """
        # Apply Snowflake-specific preprocessing (Gap 1 + Gap 3 fixes)
        sql = self._preprocess_snowflake_sql(sql)

        # Check for scripting blocks
        if self._has_scripting_block(sql):
            logger.debug("Snowflake scripting block detected in %s, using DML extraction", path)
            # Scripting path uses regex DML extraction; original line positions are lost.
            # QueryNode.start_line defaults to 0 (unknown sentinel) for these nodes.
            return self._parse_scripting_file(path, sql, rel_path=rel_path)

        # Compute start-line map from the PREPROCESSED SQL.
        # _preprocess_snowflake_sql preserves line count via lambda replacements:
        # each stripped clause (UNPIVOT, WITH TAG, ALTER … SET TAG;) is replaced
        # with exactly as many newlines as the matched text contained.  A 3-line
        # ALTER block becomes 3 blank lines — not a deletion — so the line numbers
        # of all surviving statements in the preprocessed text still reflect their
        # positions in the original file.  Computing _compute_start_lines on the
        # preprocessed SQL therefore gives correct original-file line numbers for
        # all surviving statements, even when multi-line blocks were stripped.
        #
        # Passing the map explicitly avoids AnsiParser recomputing it from `sql`
        # again (a no-op difference, but explicit is clearer and future-proof).
        preprocessed_start_lines = AnsiParser._compute_start_lines(sql)  # type: ignore[attr-defined]
        return AnsiParser.parse_file(  # type: ignore[call-arg]
            self,  # type: ignore[arg-type]
            path,
            sql,
            dependency_filter=dependency_filter,  # type: ignore[call-arg]
            ddl_columns_by_bare=ddl_columns_by_bare,  # type: ignore[call-arg]
            _precomputed_start_lines=preprocessed_start_lines,  # type: ignore[call-arg]
            rel_path=rel_path,  # type: ignore[call-arg]
        )

    @staticmethod
    def _preprocess_snowflake_sql(sql: str) -> str:
        """Apply Snowflake-specific pre-processing workarounds for sqlglot dialect gaps.

        Handles:
        1. Gap 1: Normalise CREATE TEMP TABLE t IF NOT EXISTS to CREATE TEMPORARY TABLE t
        2. Gap 3: Strip UNPIVOT clauses (destructive but acceptable for partial lineage extraction)
        3. Gap 4: Strip WITH TAG (...) clauses from column definitions
        4. Gap 4b: Strip ALTER TABLE/VIEW ... MODIFY COLUMN ... SET TAG ...; statements

        Line-count preservation contract:
            Gaps 3, 4, and 4b replace matched text with the same number of newlines
            the matched text contained (using a lambda replacement).  The Gap 1
            normalization is single-line → single-line and never changes newline count.
            Together, these guarantees ensure that after pre-processing each surviving
            statement occupies the same 1-based line number it had in the original file,
            so ``_compute_start_lines`` called on the pre-processed SQL returns correct
            original-file line numbers.

        Args:
            sql: Raw Snowflake SQL text

        Returns:
            Pre-processed SQL text with line count preserved per the contract above.
        """
        # Gap 1: CREATE TEMP/TEMPORARY TABLE name IF NOT EXISTS -> CREATE TEMPORARY TABLE name
        # This regex matches CREATE TEMP[ORARY] TABLE name IF NOT EXISTS and drops the IF NOT EXISTS
        sql = re.sub(
            r"CREATE\s+(TEMP(?:ORARY)?)\s+TABLE\s+(\w+)\s+IF\s+NOT\s+EXISTS",
            r"CREATE TEMPORARY TABLE \2",
            sql,
            flags=re.IGNORECASE,
        )

        # Gap 3: Strip UNPIVOT clauses if present
        # UNPIVOT (...) [AS alias] — strip the entire clause
        # Handle nested parentheses by counting parens
        #
        # Line-count preservation: replace the matched text with the same number
        # of newlines it contained.  This keeps all subsequent statement positions
        # in the preprocessed SQL aligned with the original file line numbers so
        # that _compute_start_lines returns correct original-file line numbers.
        if "UNPIVOT" in sql.upper():
            # Strategy: find UNPIVOT, then match everything until we close all parens
            # Use a more permissive pattern: UNPIVOT\s*\([^)]+\([^)]*\)[^)]*\)
            # This matches UNPIVOT (val FOR col IN (...)) AS alias
            sql = re.sub(
                r"\s+UNPIVOT\s*\([^)]*\([^)]*\)[^)]*\)\s*(?:AS\s+\w+)?",
                lambda m: "\n" * m.group(0).count("\n"),
                sql,
                flags=re.IGNORECASE,
            )

        # Gap 4: Strip "WITH TAG (...)" clauses from column definitions in
        # CREATE [OR REPLACE] [DYNAMIC] TABLE statements. sqlglot's Snowflake
        # dialect does not parse this clause and stalls past the 30s timeout
        # on real DWH dynamic-table DDL. Stripping the clause loses only the
        # tag metadata (we do not model it) and unblocks parsing of the AS
        # SELECT body for column lineage.
        #
        # WITH TAG may contain up to two levels of nested parens, e.g.
        #   WITH TAG (SCHEMA."tag_name"='value', other='v2')
        # A column may carry multiple WITH TAG clauses (chained). Strip each
        # occurrence independently; do not touch COMMENT or WITH MASKING
        # POLICY clauses, which sqlglot already handles.
        # Line-count preservation: replace the matched text with the same number
        # of newlines it contained so that statement positions in the preprocessed
        # SQL remain aligned with the original file (see Gap 3 note above).
        if "WITH TAG" in sql.upper():
            sql = re.sub(
                r"\s+WITH\s+TAG\s*\((?:[^()]*|\([^()]*\))*\)",
                lambda m: "\n" * m.group(0).count("\n"),
                sql,
                flags=re.IGNORECASE,
            )

        # Gap 4b: Strip ALTER TABLE/VIEW ... MODIFY COLUMN ... SET TAG ...; statements.
        # sqlglot emits these as exp.Command (unsupported syntax). They only carry tag
        # metadata we don't model, so removing them is safe and cleans the error stream.
        # Line-count preservation: replace the matched text with the same number
        # of newlines it contained so that statement positions in the preprocessed
        # SQL remain aligned with the original file (see Gap 3 note above).
        if "SET TAG" in sql.upper():
            sql = re.sub(
                r"ALTER\s+(?:DYNAMIC\s+)?(?:TABLE|VIEW)\s+[^;]*?MODIFY\s+COLUMN[^;]*?SET\s+TAG[^;]*?;",
                lambda m: "\n" * m.group(0).count("\n"),
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )

        return sql

    @staticmethod
    def _parse_with_recovery(sql: str, dialect: str | None) -> tuple[list[Any], list[str]]:
        """Parse SQL, falling back to per-chunk recovery on tokenisation failure (Gap 2).

        When the primary parse fails, splits on `;` and re-parses each chunk
        independently, collecting successful statements and recording errors
        for failed chunks.

        Args:
            sql: SQL text to parse
            dialect: Dialect string for sqlglot.parse

        Returns:
            Tuple of (statements list, recovery errors list). recovery_errors is non-empty
            when at least one chunk failed to parse.
        """
        try:
            return sqlglot.parse(sql, dialect=dialect), []
        except Exception as primary_exc:
            # Recovery: split on semicolons and parse each chunk independently
            errors = [f"parse_recovery:primary:{primary_exc}"]
            stmts: list[Any] = []
            for chunk in sql.split(";"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    parsed = sqlglot.parse(chunk, dialect=dialect)
                    stmts.extend(s for s in parsed if s is not None)
                except Exception as chunk_exc:
                    errors.append(f"parse_recovery:chunk:{chunk_exc}")
            return stmts, errors

    def _do_parse(self, sql: str) -> tuple[list[Any], list[str]]:
        """Override _do_parse to apply Snowflake-specific recovery (Gap 2).

        For Snowflake dialect, uses per-chunk recovery on parse failure.
        All other dialects use the standard AnsiParser._do_parse.

        Args:
            sql: SQL text to parse

        Returns:
            Tuple of (statements list, error strings list)
        """
        return self._parse_with_recovery(sql, self.DIALECT)

    def _transform_statements(self, statements: list[Any]) -> list[Any]:
        """Snowflake P5: track USE SCHEMA context and qualify bare table references.

        Walks the statement list once (O(N_statements)).  When a USE statement that
        sets a schema context is encountered it becomes the active schema for all
        subsequent statements in the file.  For each subsequent statement, bare
        table references (no db/catalog prefix) are qualified to ``<schema>.<name>``
        via an in-place AST mutation — ``exp.Table.db`` is set to the schema identifier.

        Supported USE forms that set the active schema:
          - ``USE SCHEMA <schema>``          — kind='SCHEMA'
          - ``USE SCHEMA <db>.<schema>``     — kind='SCHEMA', two-part name
          - ``USE <db>.<schema>``            — kind='', two-part name (W3 plan decision)

        Explicitly ignored:
          - ``USE DATABASE <db>``            — kind='DATABASE'; no schema derivable

        CTE alias names defined within each statement are excluded from qualification
        so that CTE-internal references stay unqualified and sqlglot can resolve them
        normally.

        Perf invariant: pure AST walk — no qualify/build_scope/exp.expand/sg_lineage
        calls.  The walk is O(N_tables_per_stmt) per statement, executed once per
        statement in the file-level loop.

        Args:
            statements: List of parsed sqlglot AST nodes from ``_do_parse``.

        Returns:
            The same list with USE context-setter nodes replaced by ``None`` (to skip
            them in the statement loop) and bare table refs in subsequent nodes qualified.
        """
        current_schema: str | None = None
        result: list[Any] = []

        # PR-6 Phase 1: pre-scan PropertyEQ assignments (`var := '<literal>'`) so an
        # `EXECUTE IMMEDIATE (:var)` Command node below can resolve its inner literal.
        # Only single-string-literal RHS values are captured (the statically-recoverable
        # case); a concatenation / CONCAT / bind-var RHS is not a literal and is skipped.
        var_literals: dict[str, str] = {}
        for stmt in statements:
            if isinstance(stmt, exp.PropertyEQ) and isinstance(stmt.expression, exp.Literal):
                lit = stmt.expression
                if lit.is_string and stmt.this is not None and stmt.this.name:
                    var_literals[stmt.this.name.lower()] = str(lit.this)

        for stmt in statements:
            if stmt is None:
                result.append(None)
                continue

            # PR-6 Phase 1: splice a static-literal EXECUTE IMMEDIATE inner DDL inline,
            # in place of the EXECUTE IMMEDIATE Command node, so the inner statement(s)
            # are processed by the SAME statement loop (inheriting the active USE SCHEMA
            # context and registering inner temp targets as role="temp" BEFORE any later
            # sink reads them and BEFORE the transitive temp-edge composition at :264).
            inner_stmts = self._inner_stmts_from_command(stmt, var_literals)
            if inner_stmts is not None:
                for inner in inner_stmts:
                    if inner is None:
                        continue
                    if current_schema:
                        self._qualify_bare_tables(inner, current_schema)
                    result.append(inner)
                continue

            # Detect USE SCHEMA / USE DATABASE statements (Snowflake context setters).
            # sqlglot parses these as exp.Use with args['kind'] = Var('SCHEMA') or
            # Var('DATABASE').  We track SCHEMA context; DATABASE-only context is
            # ignored (no schema can be derived from a bare database name).
            #
            # Supported forms (all set current_schema to the schema name):
            #   USE SCHEMA <schema>       → kind='SCHEMA', stmt.this.name = schema
            #   USE SCHEMA <db>.<schema>  → kind='SCHEMA', stmt.this.name = schema
            #   USE <db>.<schema>         → kind='',       stmt.this.name = schema,
            #                                              stmt.this.db   = db
            #
            # Ignored forms (not a schema setter):
            #   USE DATABASE <db>         → kind='DATABASE' — stay on current schema
            #   USE <bare_name>           → kind='', no db — Snowflake semantics:
            #                               a one-part USE sets the DATABASE; treating
            #                               it as a schema would put a database name in
            #                               the schema slot of graph keys (plan W3)
            if isinstance(stmt, exp.Use):
                kind_var = stmt.args.get("kind")
                kind_str = (kind_var.name if kind_var else "").upper()
                if stmt.this is not None and (
                    kind_str == "SCHEMA" or (not kind_str and stmt.this.db)
                ):
                    schema_table = stmt.this
                    # For "USE SCHEMA <schema>": name is the schema name directly.
                    # For "USE SCHEMA <db>.<schema>" or "USE <db>.<schema>": name is
                    # still the schema part (the rightmost component), which is what
                    # sqlglot places in Table.name when a db qualifier is present.
                    if schema_table.name:
                        current_schema = schema_table.name.lower()
                # Suppress the USE statement — it is not a real query node.
                result.append(None)
                continue

            # PR-6 Phase 1: a `var := '<literal>'` assignment is a scripting-variable
            # binding, not a query — suppress it (mirrors the USE-statement suppression).
            if isinstance(stmt, exp.PropertyEQ):
                result.append(None)
                continue

            # Qualify bare table references in this statement if we have a schema context.
            if current_schema:
                self._qualify_bare_tables(stmt, current_schema)

            result.append(stmt)

        return result

    @staticmethod
    def _qualify_bare_tables(stmt: Any, schema: str) -> None:
        """In-place: prefix all bare (no db/catalog) exp.Table refs with ``schema``.

        Excludes CTE alias names defined within ``stmt`` so that intra-statement CTE
        references remain unqualified and sqlglot can resolve them normally.

        Args:
            stmt: sqlglot AST node to mutate in place.
            schema: Lowercase schema name to prepend (e.g. ``'da'``).
        """
        # Collect CTE names defined in this statement so we can skip them.
        cte_names: set[str] = set()
        # CTEs may appear on the stmt itself (exp.Select with WITH) or on the
        # expression body (exp.Insert whose SELECT body carries WITH).
        # Guard: exp.Command nodes (unsupported syntax like ALTER EXTERNAL TABLE
        # … REFRESH, CALL, PUT, REMOVE) have .expression as a *str*, not an
        # exp.Expression, so .args would raise AttributeError.  Skip any
        # candidate that is not a proper exp.Expression node.
        for candidate in (stmt, getattr(stmt, "expression", None)):
            if candidate is None:
                continue
            if not isinstance(candidate, exp.Expression):  # type: ignore[attr-defined]
                continue
            with_clause = candidate.args.get("with_") or candidate.args.get("with")
            if with_clause is not None:
                for cte in getattr(with_clause, "expressions", []):
                    if cte.alias:
                        cte_names.add(cte.alias.lower())

        for table in stmt.find_all(exp.Table):
            # Skip if already qualified (has a db or catalog component).
            if table.args.get("db") or table.args.get("catalog"):
                continue
            # Skip CTE alias names — they are resolved within the statement scope.
            if table.name and table.name.lower() in cte_names:
                continue
            # Skip empty or synthetic names.
            if not table.name:
                continue
            # Apply the schema prefix in place.
            table.set("db", exp.Identifier(this=schema, quoted=False))

    @staticmethod
    def _scan_string_concat(text: str, start: int) -> tuple[str | None, list[int]]:
        """Scan a Snowflake string-literal concatenation starting at index ``start``.

        ``text[start]`` must be the opening ``'`` of a string literal.  Scans
        ``'lit' [ || <runtime> || 'lit' ]*`` handling backslash escapes (``\\'``)
        and doubled-quote escapes (``''``).  Each ``|| <runtime> ||`` bridge between
        two literals is replaced by ``_DYNAMIC_RUNTIME_PLACEHOLDER``.

        Args:
            text: The raw SQL text.
            start: Index of the opening quote of the first literal.

        Returns:
            ``(recovered_sql, placeholder_offsets)`` where ``placeholder_offsets`` are
            the character offsets, WITHIN ``recovered_sql``, at which a runtime
            placeholder was substituted (used by the caller to reject body-level
            concatenation).  Returns ``(None, [])`` if ``start`` does not point at a
            string literal.
        """
        n = len(text)
        if start >= n or text[start] != "'":
            return None, []
        i = start
        parts: list[str] = []
        placeholder_offsets: list[int] = []
        recovered_len = 0
        while i < n:
            if text[i] != "'":
                break
            i += 1  # consume opening quote
            buf: list[str] = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if c == "'":
                    if i + 1 < n and text[i + 1] == "'":  # doubled '' → literal quote
                        buf.append("'")
                        i += 2
                        continue
                    i += 1  # closing quote
                    break
                buf.append(c)
                i += 1
            literal = "".join(buf)
            parts.append(literal)
            recovered_len += len(literal)
            # Skip whitespace and look for a `||` concatenation continuation.
            j = i
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j + 1 < n and text[j] == "|" and text[j + 1] == "|":
                j += 2
                # Consume the runtime fragment up to the next `||`.
                while j < n and not (j + 1 < n and text[j] == "|" and text[j + 1] == "|"):
                    j += 1
                if j + 1 >= n or text[j] != "|":
                    # Unterminated concatenation — bail out (non-recoverable).
                    return None, []
                j += 2  # skip the closing `||`
                placeholder_offsets.append(recovered_len)
                parts.append(_DYNAMIC_RUNTIME_PLACEHOLDER)
                recovered_len += len(_DYNAMIC_RUNTIME_PLACEHOLDER)
                while j < n and text[j] in " \t\r\n":
                    j += 1
                i = j
                continue
            break
        return "".join(parts), placeholder_offsets

    @classmethod
    def _unwrap_execute_immediate(cls, sql: str) -> list[str]:
        """Recover static-literal inner SQL from ``EXECUTE IMMEDIATE`` sites.

        PR-6 Phase 1.  Returns the list of recovered inner SQL strings (one per
        recoverable EXECUTE IMMEDIATE).  ONLY static-literal DDL is recovered:

        - ``EXECUTE IMMEDIATE (:var)`` / ``EXECUTE IMMEDIATE :var`` where ``var`` is
          assigned a single string literal (``var := '<literal>'``).
        - inline ``EXECUTE IMMEDIATE '<literal>'``.

        A ``|| <runtime> ||`` concatenation is tolerated ONLY when it sits before the
        ``AS <body>`` (e.g. the WAREHOUSE clause) — it is replaced by a placeholder.
        A concatenation at/after the ``AS`` body marker, a CONCAT(...) call, a bind
        variable inside the literal, or a non-resolvable var makes the site
        non-recoverable and it is skipped (returns nothing for that site).

        Args:
            sql: The (preprocessed) full SQL text of the file.

        Returns:
            List of recovered inner SQL strings (may be empty).
        """
        recovered: list[str] = []
        # Track which literal-start offsets we have already consumed so the inline
        # and var-resolved passes do not double-count the same literal.
        consumed_starts: set[int] = set()

        def _try_literal_at(start: int) -> None:
            inner, placeholder_offsets = cls._scan_string_concat(sql, start)
            if inner is None:
                return
            # SCOPE GATE (plan Phase 1): only a static-literal `CREATE … AS SELECT`
            # DDL is statically recoverable. Reject anything else — SHOW / DROP /
            # bare SELECT / INSERT / UPDATE / MERGE / etc. (a SELECT/INSERT with a
            # `|| :var ||` predicate is runtime-dependent and would inject a placeholder
            # into a lineage-bearing clause). Locating the AS body anchors the
            # placeholder guard below.
            if not _CREATE_DDL_PREFIX_RE.match(inner):
                return
            body_match = _AS_BODY_START_RE.search(inner)
            if body_match is None:
                # A CREATE with no AS SELECT body carries no lineage to recover.
                # If it also has a runtime placeholder we cannot prove it is in a
                # non-lineage clause → reject. A static, body-less CREATE (e.g.
                # CREATE TABLE … LIKE …) has no SELECT and is out of scope here.
                return
            body_start = body_match.start()
            # Reject when a runtime placeholder landed at/after the AS SELECT body
            # (the body must be fully static; only a pre-AS clause such as WAREHOUSE
            # may carry a `|| <runtime> ||` placeholder — lossless for lineage).
            if any(off >= body_start for off in placeholder_offsets):
                return  # concatenation inside the body → non-recoverable
            recovered.append(inner)
            consumed_starts.add(start)

        # Pass 1 — inline literal: EXECUTE IMMEDIATE '<literal>'.
        for m in _EXEC_IMMEDIATE_LITERAL_RE.finditer(sql):
            # The lookahead positioned the cursor; find the next quote at/after m.end().
            q = sql.find("'", m.end() - 1)
            if q != -1 and q not in consumed_starts:
                _try_literal_at(q)

        # Pass 2 — variable form: EXECUTE IMMEDIATE (:var), resolve `var := '<lit>'`.
        for m in _EXEC_IMMEDIATE_VAR_RE.finditer(sql):
            var = m.group(1)
            # Find the variable's literal assignment: `var := '<literal>'`.
            assign = re.search(rf"\b{re.escape(var)}\s*:=\s*", sql, re.IGNORECASE)
            if not assign:
                continue
            q = assign.end()
            while q < len(sql) and sql[q] in " \t\r\n":
                q += 1
            if q < len(sql) and sql[q] == "'" and q not in consumed_starts:
                _try_literal_at(q)
            # else: assigned from a non-literal (concatenation / bind var) → skip.

        return recovered

    def _inner_stmts_from_command(
        self, stmt: Any, var_literals: dict[str, str]
    ) -> list[Any] | None:
        """If ``stmt`` is a static-literal EXECUTE IMMEDIATE, return its parsed inner AST.

        PR-6 Phase 1 (structured / non-scripting path).  ``EXECUTE IMMEDIATE`` parses to
        an ``exp.Command`` (``this='EXECUTE'``).  This recovers the inner SQL when it is a
        static literal — either inline (``EXECUTE IMMEDIATE '<lit>'``) or via a variable
        whose ``var := '<lit>'`` assignment was captured in ``var_literals`` — and parses
        it through ``sqlglot.parse`` so the caller can splice the result into the main
        statement loop.  Returns ``None`` when ``stmt`` is not a recoverable EXECUTE
        IMMEDIATE (so the caller leaves the original statement untouched).

        Non-recoverable cases return ``None`` (skipped, never mis-extracted):
        concatenation inside the AS body, CONCAT(...), a bind variable inside the literal,
        or a var with no captured literal assignment.

        Args:
            stmt: A parsed sqlglot AST node.
            var_literals: ``var-name(lower) → literal RHS`` map pre-scanned from
                ``PropertyEQ`` assignments in the same file.

        Returns:
            The parsed inner statement list, or ``None`` when not applicable.
        """
        if not isinstance(stmt, exp.Command):
            return None
        if (getattr(stmt, "name", "") or "").upper() != "EXECUTE":
            return None
        # The Command text round-trips the original `EXECUTE IMMEDIATE ...` source.
        command_text = stmt.sql(dialect=self.DIALECT)
        if "IMMEDIATE" not in command_text.upper():
            return None

        inner_sql: str | None = None
        # Inline literal form: recover directly from the command text.
        recovered = self._unwrap_execute_immediate(command_text)
        if recovered:
            inner_sql = recovered[0]
        else:
            # Variable form: EXECUTE IMMEDIATE (:var) — resolve the captured literal.
            var_m = _EXEC_IMMEDIATE_VAR_RE.search(command_text)
            if var_m is not None:
                literal = var_literals.get(var_m.group(1).lower())
                if literal is not None:
                    # Re-run the literal scanner over the assignment's RHS so a
                    # WAREHOUSE-clause concatenation placeholder / body-concat guard
                    # applies identically to the variable form. The captured RHS is the
                    # already-unescaped literal value, so wrap it back in quotes for the
                    # scanner only when it still contains a `||` bridge; otherwise the
                    # raw value IS the inner SQL.
                    inner_sql = literal
        if inner_sql is None:
            return None

        try:
            return sqlglot.parse(inner_sql, dialect=self.DIALECT)
        except Exception:
            return None

    def _emit_dynamic_sql_statements_scripting(
        self,
        sql: str,
        path: Path,
        out: ParsedFile,
        sources_map: dict[str, Any],
        active_schema: str | None,
    ) -> None:
        """Surface static-literal EXECUTE IMMEDIATE inner DDL on the SCRIPTING path.

        PR-6 Phase 1.  A ``DECLARE/BEGIN`` block (e.g. the WTFA dynamic-table file)
        cannot be parsed at the top level, so the structured ``_transform_statements``
        splice never runs.  This raw-text recovery (see ``_unwrap_execute_immediate``)
        parses each recovered inner statement through the normal
        ``AnsiParser._parse_statement`` path — inner temp targets register
        ``role="temp"`` and column lineage is extracted exactly as for a top-level
        statement — and APPENDS the resulting QueryNode(s) into ``out.statements`` so the
        transitive temp-edge composition (Phase 2) sees them.

        Args:
            sql: The (preprocessed) full SQL text of the file.
            path: Path to the source file.
            out: ParsedFile to append recovered inner QueryNodes to.
            sources_map: Shared temp/CTAS body map.
            active_schema: Schema active at the EXECUTE IMMEDIATE site (USE SCHEMA reuse).
        """
        inner_sqls = self._unwrap_execute_immediate(sql)
        if not inner_sqls:
            return

        stmt_index = len(out.statements)
        for inner_sql in inner_sqls:
            try:
                inner_statements = sqlglot.parse(inner_sql, dialect=self.DIALECT)
            except Exception as exc:
                out.errors.append(f"dynamic_sql_parse_error:{exc}")
                continue
            for stmt in inner_statements:
                if stmt is None:
                    continue
                if active_schema:
                    self._qualify_bare_tables(stmt, active_schema)
                try:
                    query_node: Any = AnsiParser._parse_statement(  # type: ignore[arg-type]
                        self,
                        stmt,
                        path,
                        stmt_index,
                        out,
                        sources_map,
                    )
                    query_node.parsing_mode = "dynamic_sql"
                    out.statements.append(query_node)

                    # Register CTAS/temp bodies so a later sink resolves the temp.
                    if isinstance(stmt, exp.Create):
                        _expr = stmt.expression
                        if isinstance(_expr, exp.Subquery):
                            _expr = _expr.this
                        if isinstance(_expr, exp.Select) and query_node.target:
                            target_name = (query_node.target.name or "").lower()
                            if target_name:
                                sources_map[target_name] = _expr

                    if query_node.kind in ("CREATE_TABLE", "CREATE_VIEW"):
                        if query_node.target:
                            out.defined_tables.append(query_node.target)
                    out.referenced_tables.extend(query_node.sources)
                    if query_node.column_lineage:
                        out.parse_quality = ParseQuality.FULL
                    stmt_index += 1
                except Exception as exc:
                    out.errors.append(f"dynamic_sql_statement_error:{exc}")

    def _has_scripting_block(self, sql: str) -> bool:
        """Token-aware BEGIN detection — avoids false-positives on string literals and comments.

        The sqlglot Tokenizer is instantiated as ``Tokenizer(dialect="snowflake")``
        (constructor-kwarg form) because ``Tokenizer.from_dialect(...)`` does not exist
        in the version of sqlglot bundled with this project (30.x).  Using the wrong
        factory call causes an AttributeError that is silently swallowed by the ``except``
        clause, falling back to a plain ``re.search`` that matches ``BEGIN`` inside SQL
        comments — producing false-positive scripting-block detection and routing the
        file through ``_parse_scripting_file``, where ``CREATE TEMPORARY TABLE`` is not
        extracted, so ``_current_file_temp_keys`` is never populated and downstream
        INSERT edges use un-namespaced temp keys (the v1.21.1 dual-write bug class).

        Args:
            sql: SQL text to check

        Returns:
            True if a scripting block is detected
        """
        try:
            from sqlglot.tokens import Tokenizer, TokenType  # type: ignore

            toks = Tokenizer(dialect="snowflake").tokenize(sql)  # type: ignore
            return any(t.token_type == TokenType.BEGIN for t in toks)  # type: ignore
        except Exception:
            # Fallback to regex if tokenization fails
            return bool(_SCRIPTING_BLOCK.search(sql))

    def _parse_scripting_file(
        self, path: Path, sql: str, rel_path: str | None = None
    ) -> ParsedFile:
        """Parse a Snowflake file with scripting blocks using DML extraction.

        USE SCHEMA context (Step 3.1, plan/sprints/sprint_lineage_identity_and_session_context.md):
        The scripting path bypasses ``_transform_statements`` because sqlglot cannot
        parse ``USE`` inside a ``BEGIN…END`` block.  Instead, this method performs a
        position-aware pre-scan of the raw SQL text for USE schema-setter statements
        (``_USE_SCHEMA_RE``), building a list of ``(offset, schema)`` pairs.  For each
        ``_EMBEDDED_DML`` match, the schema active at that text offset is looked up and
        ``_qualify_bare_tables`` is called on the parsed AST before ``_parse_statement``
        extracts table references.

        USE DATABASE <x> and bare one-part USE <x> are ignored (a one-part USE sets
        the database in Snowflake — no schema is derivable).  False positives (USE
        inside a string/comment) are bounded and acceptable for ETL code (see
        module-level ``_USE_SCHEMA_RE`` comment).

        Args:
            path: Path to the source file
            sql: SQL text to parse

        Returns:
            ParsedFile with extracted DML statements

        Note:
            dependency_filter is not applied here — DML extraction bypasses sources_map seeding;
            full xfile_sources used intentionally.
        """
        out = ParsedFile(path=path, dialect=self.DIALECT)
        out.parse_quality = ParseQuality.SCRIPTING_FALLBACK
        out.errors.append("parse_mode:scripting_block")

        # PR 3 (sprint_postmortem_fixes §PR 3 Step 3.1) — use repo-relative posix path when
        # available so CTE/derived keys are portable.  Falls back to str(path) for single-file
        # callers that do not supply rel_path.
        self._current_file_namespace = rel_path if rel_path is not None else str(path)
        # temp_table_namespacing (Step 1.1 / W4): reset alongside _current_file_namespace
        # at the _parse_scripting_file entry point (the SECOND reset site in the Snowflake
        # parser).  Without this, a pool-reused parser would leak temp keys from the
        # previous file into this scripting block's reads.
        self._current_file_temp_keys = set()

        # --- Step 3.1: build position-aware USE schema context map ---
        # Scan the raw SQL for USE schema-setter statements and record (offset, schema).
        # The list is sorted by offset (finditer guarantees left-to-right order).
        # For each DML match we pick the last entry whose offset is < match.start().
        use_contexts: list[tuple[int, str]] = []
        for use_m in _USE_SCHEMA_RE.finditer(sql):
            # Schema is in group 1 (SCHEMA-keyword form) or group 2 (USE db.schema).
            # USE DATABASE / bare one-part USE never match the regex (see its comment).
            schema_name = (use_m.group(1) or use_m.group(2)).lower()
            use_contexts.append((use_m.start(), schema_name))

        def _active_schema(offset: int) -> str | None:
            """Return the schema name active at ``offset``, or None."""
            active: str | None = None
            for pos, name in use_contexts:
                if pos < offset:
                    active = name
                else:
                    break
            return active

        # Initialize sources_map for temp table resolution.
        # Seed with cross-file CTAS bodies from pass 1 (intra-file overrides).
        xfile_sources = dict(self._schema.cross_file_sources()) if self._schema else {}
        sources_map: dict[str, Any] = xfile_sources

        # Extract DML statements using regex
        stmt_index = 0

        for match in _EMBEDDED_DML.finditer(sql):
            dml_sql = match.group(1).strip()
            if not dml_sql:
                continue

            # Determine USE schema context active at this DML statement's offset.
            active_schema = _active_schema(match.start())

            try:
                # Try to parse the extracted DML
                statements = sqlglot.parse(dml_sql, dialect=self.DIALECT)
                for stmt in statements:
                    if stmt is None:
                        continue

                    # Apply USE SCHEMA qualification before _parse_statement so that
                    # table-ref extraction sees the qualified names.
                    if active_schema:
                        self._qualify_bare_tables(stmt, active_schema)

                    try:
                        # Call parent's _parse_statement method
                        query_node: Any = AnsiParser._parse_statement(  # type: ignore
                            self,
                            stmt,
                            path,
                            stmt_index,
                            out,
                            sources_map,
                        )
                        # Mark as parse_failed since we're in scripting mode
                        query_node.parse_failed = True
                        query_node.confidence = 0.3
                        query_node.parsing_mode = "scripting"
                        out.statements.append(query_node)

                        # Register CTAS bodies in sources_map for downstream temp table refs
                        if isinstance(stmt, exp.Create):
                            _expr = stmt.expression
                            if isinstance(_expr, exp.Subquery):
                                _expr = _expr.this
                            if isinstance(_expr, exp.Select) and query_node.target:
                                target_name = (query_node.target.name or "").lower()
                                if target_name:
                                    sources_map[target_name] = _expr

                        stmt_index += 1

                        # Track table references
                        if query_node.kind in ("CREATE_TABLE", "CREATE_VIEW"):
                            if query_node.target:
                                out.defined_tables.append(query_node.target)
                        out.referenced_tables.extend(query_node.sources)

                    except Exception as exc:
                        logger.debug(
                            "Failed to process extracted DML statement %d in %s: %s",
                            stmt_index,
                            path,
                            exc,
                        )
                        out.errors.append(f"statement_error:{stmt_index}:{exc}")
                        stmt_index += 1

            except Exception as exc:
                logger.debug("Failed to parse extracted DML from %s: %s", path, exc)
                out.errors.append(f"dml_extraction_error:{exc}")

        # PR-6 (dynamic-SQL EXECUTE IMMEDIATE unwrap, Phase 1): the scripting path is
        # how a DECLARE/BEGIN block (the WTFA dynamic-table file) reaches the parser.
        # Surface any static-literal EXECUTE IMMEDIATE inner DDL into out.statements so
        # the recovered CREATE produces a SqlQuery + SELECTS_FROM edges and inner temp
        # targets register role="temp".  Then compose transitive temp edges (Phase 2) —
        # this is the ONLY _emit_transitive_temp_edges call on the scripting path, which
        # otherwise never reaches the AnsiParser post-loop composition at :264.
        self._emit_dynamic_sql_statements_scripting(
            sql, path, out, sources_map, _active_schema(len(sql))
        )
        AnsiParser._emit_transitive_temp_edges(self, out)  # type: ignore[arg-type]

        # PR 3 — clear namespace after parsing
        self._current_file_namespace = None
        # temp_table_namespacing (Step 1.1 / W4): clear temp-key set at scripting file end.
        self._current_file_temp_keys = set()
        return out
