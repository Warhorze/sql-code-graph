"""Cross-file lineage aggregator for two-pass resolution."""

from typing import Any

from sqlcg.parsers.base import ParsedFile
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


class CrossFileAggregator:
    """Aggregates parsed files for cross-file resolution.

    Pass 1: register all parsed files and build view/table source mappings.
    Pass 2: re-parse files with cross-file schema context.
    """

    def __init__(self) -> None:
        """Initialize the aggregator."""
        # Maps table.full_id -> ParsedFile that defines it
        self.sources: dict[str, ParsedFile] = {}
        # Maps lowercased table name (bare name) -> exp.Select body for CTAS statements.
        # Populated during register_pass1 and used to seed sources_map in pass 2.
        self.cross_file_sources: dict[str, Any] = {}
        # #44 canonical-name index: bare name (lowercased) -> sole DDL-defined full_id.
        # Built from DDL-defined tables only (defined_tables, not CTAS bodies).
        # Used by _build_file_rows to rewrite an unqualified INSERT-target to the
        # canonical full_id so INSERT-target nodes share identity with the DDL node.
        self.canonical_by_bare: dict[str, str] = {}
        # Bare names defined by >1 schema — do NOT rewrite (ambiguous).
        self._ambiguous_bare: set[str] = set()

    def register_pass1(self, parsed: ParsedFile) -> None:
        """Register a pass-1 result and build view/table source map.

        Also harvests CTAS bodies from statements for cross-file temp-table resolution,
        and builds the bare-name → canonical-full_id index (#44) from DDL tables.

        Args:
            parsed: ParsedFile from pass 1
        """
        for table in parsed.defined_tables:
            self.sources[table.full_id] = parsed
            # #44: build canonical_by_bare index from DDL-defined tables
            bare = (table.name or "").lower()
            if bare:
                if bare in self.canonical_by_bare and self.canonical_by_bare[bare] != table.full_id:
                    # Same bare name defined in multiple schemas → ambiguous, never rewrite
                    self._ambiguous_bare.add(bare)
                else:
                    self.canonical_by_bare[bare] = table.full_id

        # Harvest CTAS bodies from statements for cross-file resolution.
        # Key convention matches AnsiParser.parse_file line 109: lowercased bare name.
        for stmt_node in parsed.statements:
            if stmt_node.target and stmt_node.defined_body is not None:
                key = (stmt_node.target.name or "").lower()
                if key:
                    self.cross_file_sources[key] = stmt_node.defined_body

    def _needs_pass2(self, parsed: ParsedFile) -> bool:
        """Return True iff pass-2 re-parse would see new schema context for this file.

        A file benefits from pass 2 iff at least one of its statement sources or
        cross-file dependency references a table registered by another file's
        pass-1 result (self.sources) or a CTAS body harvested from another file
        (self.cross_file_sources).

        Files that reference only same-file tables, or only tables absent from
        both registries, gain nothing — pass 1 already had all the schema context
        available. Skipping the re-parse for these files is observably equivalent
        to keeping it (verified by the equivalence test in tests/unit/test_aggregator_skip.py).
        """
        same_file_table_ids = {t.full_id for t in parsed.defined_tables}
        same_file_bare_names = {t.name.lower() for t in parsed.defined_tables}

        for stmt in parsed.statements:
            for src in stmt.sources:
                # Cross-file by full_id?
                if src.full_id in self.sources and src.full_id not in same_file_table_ids:
                    return True
                # Cross-file by bare name (CTAS body lookup)?
                bare = (src.name or "").lower()
                if bare and bare in self.cross_file_sources and bare not in same_file_bare_names:
                    return True
        return False

    def resolve_pass2(self, parser, parsed: ParsedFile) -> ParsedFile:
        """Re-parse with cross-file schema context.

        Args:
            parser: SqlParser instance
            parsed: ParsedFile from pass 1

        Returns:
            ParsedFile from pass 2 with resolved cross-file references,
            or the pass-1 result if skip predicate determines no re-parse is needed
            or if the file cannot be re-read.

        Raises:
            No exceptions are raised; file read errors are logged as WARNING
            and the pass-1 result is returned unchanged.

        Note:
            This method returns the exact same ParsedFile object (via `return parsed`)
            on the skip path. This identity semantics are used by callers to track
            which files were skipped (resolved is parsed). Do not introduce a .copy()
            on the skip path — that would break the identity check.
        """
        # Register view sources for schema resolution
        parser._schema.add_view_sources(self.sources)

        if not self._needs_pass2(parsed):
            # File has no cross-file dependencies — pass-1 result is already final.
            return parsed

        try:
            sql = parsed.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "resolve_pass2: cannot re-read %s (%s) — returning pass-1 result",
                parsed.path,
                exc,
            )
            return parsed

        # Filter cross-file CTAS bodies to what this file actually references —
        # keeps exp.expand bounded by referenced_tables, not by corpus size.
        ref_names = {(t.name or "").lower() for t in parsed.referenced_tables if t.name}
        return parser.parse_file(parsed.path, sql, dependency_filter=ref_names)
