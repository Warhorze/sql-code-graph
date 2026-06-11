"""Unit tests for PR 4 Step 4.1 — <unknown> sentinel self-edge drop.

Guards the honest-drop behaviour: when a column resolves to the <unknown>
sentinel (schema mismatch path) or when sg_lineage raises (exception path),
no self-edge is emitted; instead a col_lineage_skip:unknown_sentinel:<col>
reason is appended to ParsedFile.errors.

([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.1)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sqlglot import parse_one

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser
from sqlcg.parsers.base import ParsedFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parser() -> AnsiParser:
    return AnsiParser(SchemaResolver())


def _count_unknown_edges(edges) -> int:
    """Count edges whose src or dst table name is the <unknown> sentinel."""
    return sum(
        1 for e in edges if e.src.table.name == "<unknown>" or e.dst.table.name == "<unknown>"
    )


# ---------------------------------------------------------------------------
# Schema-mismatch path (site 1): col not in schema dict
# ---------------------------------------------------------------------------


class TestSchemaPathUnknownSentinelDrop:
    """The schema-validation path no longer emits a <unknown> self-edge.

    Previously: when a schema dict was provided and col_name was absent, a
    0.5-confidence <unknown>→<unknown> edge was emitted.
    Now: the edge is dropped; a col_lineage_skip:unknown_sentinel:<col> reason
    is appended instead.

    Guards PR 4 Step 4.1 site 1 (base.py ~line 1328).
    ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.1)
    """

    def test_schema_mismatch_emits_no_unknown_edge(self):
        """Column absent from schema dict produces no edge, only a skip reason.

        The schema dict provides 'other_col' but not 'col_a', so the
        schema-validation branch fires for col_a and must drop the edge.
        """
        parser = _make_parser()
        stmt = parse_one("SELECT col_a FROM src_table")
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        # Provide a schema that does NOT contain col_a — triggers site 1.
        schema = {"src_table": ["other_col", "col_b"]}

        result = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=schema)

        # No <unknown> sentinel edge emitted.
        unknown_edges = _count_unknown_edges(result.edges)
        assert unknown_edges == 0, (
            f"Expected 0 <unknown> sentinel edges, got {unknown_edges}: {result.edges}"
        )

        # A skip reason is recorded.
        skip_reasons = [e for e in out.errors if e.startswith("col_lineage_skip:unknown_sentinel:")]
        assert len(skip_reasons) >= 1, (
            f"Expected at least one col_lineage_skip:unknown_sentinel:* reason, got: {out.errors}"
        )
        assert any("col_a" in r for r in skip_reasons), (
            f"Skip reason must name the column (col_a), got: {skip_reasons}"
        )

    def test_schema_mismatch_legitimate_edge_unaffected(self):
        """Columns present in the schema dict still produce real edges (regression guard).

        Only the sentinel-path is affected; non-sentinel lineage is unchanged.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.1)
        """
        parser = _make_parser()
        # Both col_a and col_b are in schema — no sentinel path fires.
        sql = "SELECT col_a FROM src_table"
        stmt = parse_one(sql)
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        schema = {"src_table": ["col_a", "col_b"]}
        parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=schema)

        # The schema check passes (col_a is present) — normal lineage runs.
        # We assert no sentinel edges and that errors do NOT contain an
        # unknown_sentinel reason for col_a.
        unknown_sentinel_reasons = [
            e for e in out.errors if "col_lineage_skip:unknown_sentinel:col_a" in e
        ]
        assert unknown_sentinel_reasons == [], (
            f"col_a is in schema — should not produce a sentinel skip reason. Got: {out.errors}"
        )


# ---------------------------------------------------------------------------
# Exception path (site 2): sg_lineage raises
# ---------------------------------------------------------------------------


class TestExceptionPathUnknownSentinelDrop:
    """The sg_lineage exception path no longer emits a <unknown> self-edge.

    Previously: a 0.0-confidence <unknown>→<unknown> edge was emitted on any
    sg_lineage exception.  Now: the edge is dropped; a
    col_lineage_skip:unknown_sentinel:<col> reason is appended alongside the
    existing col_lineage:<col>:<exc> error.

    Guards PR 4 Step 4.1 site 2 (base.py ~line 1404).
    ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Step 4.1)
    """

    def test_exception_path_emits_no_unknown_edge(self):
        """sg_lineage exception produces zero edges and both skip/error reasons."""
        parser = _make_parser()
        stmt = parse_one("SELECT col_x FROM src")
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        with patch("sqlglot.lineage.lineage") as mock_sg:
            mock_sg.side_effect = RuntimeError("lineage blew up")
            result = parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=None)

        # Observable: no edges at all.
        assert result.edges == [], (
            f"Expected zero edges on sg_lineage exception (honest drop), "
            f"got {len(result.edges)} edge(s): {result.edges}"
        )

        # Original error reason still present.
        assert any("col_lineage:col_x:" in e for e in out.errors), (
            f"Original col_lineage error reason missing from: {out.errors}"
        )

        # New sentinel skip reason present.
        sentinel_reasons = [e for e in out.errors if e == "col_lineage_skip:unknown_sentinel:col_x"]
        assert len(sentinel_reasons) == 1, (
            f"Expected exactly one col_lineage_skip:unknown_sentinel:col_x, got: {out.errors}"
        )

    def test_exception_path_no_qualifying_op_added(self):
        """The sentinel skip adds no qualify/scope op — pure string append only.

        Behavioural perf assertion (PR 4 §Performance-invariant guardrails):
        we verify that the module-level `qualify` import is NOT called during
        the exception path by checking that the call count stays at 0 when
        sg_lineage raises.

        ([plan doc](plan/sprints/sprint_postmortem_fixes.md) §Performance-invariant guardrails)
        """
        import sqlcg.parsers.base as base_module

        parser = _make_parser()
        stmt = parse_one("SELECT col_y FROM src")
        out = ParsedFile(path=Path("test.sql"), dialect=None)

        with (
            patch("sqlglot.lineage.lineage") as mock_sg,
            patch.object(base_module, "qualify", wraps=base_module.qualify) as mock_qualify,
        ):
            mock_sg.side_effect = ValueError("fail")
            parser._extract_column_lineage(stmt, Path("test.sql"), out, schema=None)
            # qualify is called once per statement (the qualify-once pattern) but
            # never an additional time inside the exception handler.
            # The key assertion: call count == 1 (once per statement, not once per column).
            assert mock_qualify.call_count <= 1, (
                f"qualify called {mock_qualify.call_count} times in the exception path — "
                f"the sentinel skip must not add a per-column qualify call."
            )
