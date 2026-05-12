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

    def register_pass1(self, parsed: ParsedFile) -> None:
        """Register a pass-1 result and build view/table source map.

        Also harvests CTAS bodies from statements for cross-file temp-table resolution.

        Args:
            parsed: ParsedFile from pass 1
        """
        for table in parsed.defined_tables:
            self.sources[table.full_id] = parsed

        # Harvest CTAS bodies from statements for cross-file resolution.
        # Key convention matches AnsiParser.parse_file line 109: lowercased bare name.
        for stmt_node in parsed.statements:
            if stmt_node.target and stmt_node.defined_body is not None:
                key = (stmt_node.target.name or "").lower()
                if key:
                    self.cross_file_sources[key] = stmt_node.defined_body

    def resolve_pass2(self, parser, parsed: ParsedFile) -> ParsedFile:
        """Re-parse with cross-file schema context.

        Args:
            parser: SqlParser instance
            parsed: ParsedFile from pass 1

        Returns:
            ParsedFile from pass 2 with resolved cross-file references,
            or the pass-1 result if the file cannot be re-read.

        Raises:
            No exceptions are raised; file read errors are logged as WARNING
            and the pass-1 result is returned unchanged.
        """
        # Register view sources for schema resolution
        parser._schema.add_view_sources(self.sources)

        try:
            sql = parsed.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "resolve_pass2: cannot re-read %s (%s) — returning pass-1 result",
                parsed.path,
                exc,
            )
            return parsed

        return parser.parse_file(parsed.path, sql)
