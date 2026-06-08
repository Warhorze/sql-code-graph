"""Cross-file lineage aggregator for two-pass resolution."""

from typing import Any

from sqlcg.parsers.base import ParsedFile, TableRef
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)

# Bounded hop limit for CLONE-of-CLONE chain resolution (R7 — cycle/runaway-chain
# guard, plan/sprints/positional_insert_clone_blindspot.md Part C2). Chosen well
# above any plausible real-world CLONE depth; a chain this deep is itself a smell
# and degrading to "inherit nothing" is the correct, honest outcome.
_CLONE_CHAIN_HOP_LIMIT = 16


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
        # Part B-catalog: bare name (lowercased) -> ordered DDL column list, sourced
        # from CREATE TABLE/VIEW column-list DDL (StatementNode.defined_columns,
        # already ordered + lowercased). Ambiguous bare names are excluded — never
        # guess a catalog across schemas (R1/R8). Shared by Part B (no-column-list
        # positional-INSERT ordinal mapping) and Part C (CLONE inheritance feeds
        # inherited entries into this same index via resolve_clone_catalogs, so
        # both consult one source of truth)
        # (plan/sprints/positional_insert_clone_blindspot.md Part B-catalog / C2).
        self.ddl_columns_by_bare: dict[str, list[str]] = {}
        # Part C1/C2: bare name (lowercased) -> (clone_target_full_id, clone_source TableRef)
        # for every CLONE create whose target has no own column-list DDL. Collected
        # during register_pass1 (CREATE statements are visible in pass 1); resolved
        # by resolve_clone_catalogs() before pass-2 dispatch (R6 sequencing).
        self._clone_targets: dict[str, tuple[str, TableRef]] = {}
        # C2 emission: bare name -> ordered inherited column list, populated by
        # resolve_clone_catalogs() for targets whose chain resolved to a real DDL
        # catalog. Consumed by the main-process upsert seam to emit SqlColumn /
        # HAS_COLUMN rows tagged source="clone_inherited" (recommended emission
        # strategy — bypasses defined_by_query, never mutates QueryNode in place).
        self.clone_inherited_columns: dict[str, list[str]] = {}

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
        # Also build the Part-B-catalog ordered DDL-column index (bare -> ordered cols)
        # and collect CLONE targets for Part C2 chain resolution — both read the same
        # CREATE statements harvested here, in the same single pass-1 walk
        # (plan/sprints/positional_insert_clone_blindspot.md Part B-catalog / C1/C2).
        for stmt_node in parsed.statements:
            if stmt_node.target and stmt_node.defined_body is not None:
                key = (stmt_node.target.name or "").lower()
                if key:
                    self.cross_file_sources[key] = stmt_node.defined_body

            if (
                stmt_node.target
                and stmt_node.kind in ("CREATE_TABLE", "CREATE_VIEW")
                and stmt_node.defined_columns
            ):
                bare = (stmt_node.target.name or "").lower()
                if bare:
                    if bare in self.ddl_columns_by_bare:
                        if self.ddl_columns_by_bare[bare] != stmt_node.defined_columns:
                            self._ambiguous_bare.add(bare)
                            self.ddl_columns_by_bare.pop(bare, None)
                    elif bare not in self._ambiguous_bare:
                        self.ddl_columns_by_bare[bare] = list(stmt_node.defined_columns)

            if (
                stmt_node.target
                and stmt_node.kind == "CREATE_TABLE"
                and stmt_node.clone_source is not None
                and not stmt_node.defined_columns
            ):
                bare = (stmt_node.target.name or "").lower()
                if bare:
                    self._clone_targets[bare] = (stmt_node.target.full_id, stmt_node.clone_source)

        # Re-exclude any bare name that became ambiguous after this file's DDL
        # registration (mirrors canonical_by_bare's same-pass correction).
        for bare in self._ambiguous_bare:
            self.ddl_columns_by_bare.pop(bare, None)

    def resolve_clone_catalogs(self) -> None:
        """Inherit CLONE-source column catalogs onto CLONE targets (Part C2).

        Runs ONCE, at the pass-1 -> pass-2 boundary (after all register_pass1 calls,
        before pass-2 dispatch) — the preferred R6 sequencing resolution recorded in
        Phase 0: CLONE sources are DDL tables registered in pass 1, so their catalogs
        are already in `ddl_columns_by_bare` by the time this runs, and extending that
        same index here means Part B's parse-time (pass-2) catalog read sees the
        inherited entries too.

        For each CLONE target with no own column-list DDL:
          - resolve its source's ordered DDL columns from `ddl_columns_by_bare` by
            bare name (ambiguous bare names are already excluded — R8);
          - follow CLONE-of-CLONE chains transitively via `_clone_targets`, with a
            visited-set cycle guard and a bounded hop limit (`_CLONE_CHAIN_HOP_LIMIT`,
            R7) — on cycle/limit/no-DDL-found, inherit nothing;
          - an un-indexed source (cross-DB CLONE, bare name absent from the index)
            inherits nothing — the table stays catalog-less and A1/A2 label it.

        Resolved catalogs are written to BOTH `ddl_columns_by_bare` (so Part B's
        ordinal mapping finds them) and `clone_inherited_columns` (so the
        main-process upsert seam can emit `SqlColumn`/`HAS_COLUMN` rows tagged
        `source="clone_inherited"` — the recommended C2 emission strategy, which
        bypasses `defined_by_query` and never mutates `QueryNode` in place).

        Cost: O(N_clone_targets x chain_depth) dict lookups — no qualify, no
        build_scope, no exp.expand, no sg_lineage, no per-column work
        (plan/sprints/positional_insert_clone_blindspot.md Part C2 perf analysis).
        """
        for bare, (_target_full_id, _source_ref) in self._clone_targets.items():
            if bare in self.ddl_columns_by_bare:
                # Own DDL catalog already present (or resolved by an earlier
                # iteration in a chain) — its own catalog wins, no inheritance.
                continue

            resolved = self._resolve_clone_chain(bare)
            if resolved:
                self.ddl_columns_by_bare[bare] = resolved
                self.clone_inherited_columns[bare] = resolved

    def clone_inherited_catalog_rows(self) -> list[tuple[TableRef, list[str]]]:
        """Return (clone_target_ref, inherited_ordered_columns) for resolved CLONE targets.

        Consumed by the main-process upsert seam to emit `SqlColumn`/`HAS_COLUMN`
        rows for each CLONE target whose catalog was inherited (tagged
        `source="clone_inherited"`) — the recommended C2 emission strategy: row-level
        emission straight from this resolved index, bypassing `defined_by_query`
        entirely and never touching `QueryNode.defined_columns`
        (plan/sprints/positional_insert_clone_blindspot.md Part C2 step 4).

        Must be called AFTER `resolve_clone_catalogs()`. Returns an empty list when
        no CLONE target resolved a catalog (degrade path — A1/A2 label those tables).
        """
        rows: list[tuple[TableRef, list[str]]] = []
        for bare, cols in self.clone_inherited_columns.items():
            entry = self._clone_targets.get(bare)
            if entry is None or not cols:
                continue
            full_id, _source_ref = entry
            target_ref = self._table_ref_for_full_id(full_id)
            if target_ref is not None:
                rows.append((target_ref, cols))
        return rows

    @staticmethod
    def _table_ref_for_full_id(full_id: str) -> TableRef | None:
        """Reconstruct a TableRef from a dotted full_id (catalog.db.name / db.name / name)."""
        if not full_id:
            return None
        parts = full_id.split(".")
        if len(parts) == 3:
            return TableRef(catalog=parts[0], db=parts[1], name=parts[2])
        if len(parts) == 2:
            return TableRef(db=parts[0], name=parts[1])
        return TableRef(name=parts[0])

    def _resolve_clone_chain(self, start_bare: str) -> list[str] | None:
        """Follow a CLONE-of-CLONE chain from `start_bare` to a DDL donor catalog.

        Returns the ordered column list of the chain's terminal DDL donor, or None
        when the chain bottoms out in an un-indexed source, a catalog-less table,
        a cycle, or exceeds the hop limit (R7/R8/R9 degrade — inherit nothing).
        """
        visited: set[str] = set()
        current = start_bare
        for _hop in range(_CLONE_CHAIN_HOP_LIMIT):
            if current in visited:
                return None  # cycle guard (R7)
            visited.add(current)

            entry = self._clone_targets.get(current)
            if entry is None:
                # `current` is not itself a CLONE target — it's either a real DDL
                # donor (catalog already known) or an un-indexed/catalog-less table.
                return self.ddl_columns_by_bare.get(current)

            _full_id, source_ref = entry
            source_bare = (source_ref.name or "").lower()
            if not source_bare:
                return None

            # The source already has a resolved catalog (direct DDL or previously
            # resolved CLONE) — done.
            direct = self.ddl_columns_by_bare.get(source_bare)
            if direct is not None:
                return direct

            current = source_bare

        return None  # hop limit exceeded (R7) — inherit nothing

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
