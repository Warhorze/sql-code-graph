"""Deterministic perf-regression guard: hot-path ops must not go super-linear.

WHY THIS EXISTS
---------------
Every perf blow-up this project has hit (parser 3 min -> 40 min, 7 min
wide-SELECT, 1020 s upsert) was the *same class of bug*: an operation that
must run **once per statement** (build_scope / qualify) started scaling with
column count or edge count — i.e. O(N) flipped to O(N^2).

The named-instance tests (test_T09_01_qualify_once, test_bulk_upsert_invariant)
each pin ONE known regression. They cannot catch the *next* one of the same
shape. This guard does: it counts the expensive operations while parsing a
synthetic fixture at size N and 2N and asserts they do not grow super-linearly.

Operation counts are DETERMINISTIC — unlike wall-clock time, they do not vary by
machine, so this test never flakes and never gets @skip'd for being noisy. A
failure here means a CLAUDE.md "Performance invariants" entry was broken; do not
mark it pre-existing and move on — find what started scaling.

WHAT THE COLUMN-COUNT AXIS CATCHES
----------------------------------
parse_file at 2x columns with the same statement count: build_scope / qualify
must stay FLAT (once per statement). Catches per-column qualify (the 7-min
wide-SELECT). sg_lineage is per-column by design, so only its slope is bounded
(~linear).
"""

from contextlib import contextmanager
from pathlib import Path

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser

# Fixture sizes: large enough that a per-item regression dwarfs the slack,
# small enough that the whole test runs in well under a second.
COLS_BASE, COLS_2X = 24, 48


class HotOpCounters:
    """Tallies of the operations whose super-linear growth caused past blow-ups."""

    def __init__(self) -> None:
        self.build_scope = 0
        self.qualify = 0
        self.sg_lineage = 0


@contextmanager
def count_hot_ops():
    """Patch every hot-path op at its real binding site and tally calls.

    CRITICAL: build_scope/qualify are counted ONLY at sqlcg's binding site
    (base.build_scope / base.qualify — the module globals base.py calls), NOT at
    the sqlglot source module. sg_lineage() internally calls sqlglot's own
    qualify once per column by design; patching the source would conflate that
    legitimate per-column machinery with base.py's once-per-statement invariant
    and produce a false super-linear reading.

    sg_lineage IS imported locally from its source module inside
    _extract_column_lineage, so it is patched at the source. No single call
    passes through two wrappers — no double counting.
    """
    import sqlglot.lineage as sqlglot_lin

    import sqlcg.parsers.base as base_mod

    c = HotOpCounters()

    orig = {
        "lin_lineage": sqlglot_lin.lineage,
        "base_bs": base_mod.build_scope,
        "base_q": base_mod.qualify,
    }

    def lineage_wrap(*a, **k):
        c.sg_lineage += 1
        return orig["lin_lineage"](*a, **k)

    def build_scope_wrap_factory(key):
        def wrap(*a, **k):
            c.build_scope += 1
            return orig[key](*a, **k)

        return wrap

    def qualify_wrap_factory(key):
        def wrap(*a, **k):
            c.qualify += 1
            return orig[key](*a, **k)

        return wrap

    sqlglot_lin.lineage = lineage_wrap
    base_mod.build_scope = build_scope_wrap_factory("base_bs")
    base_mod.qualify = qualify_wrap_factory("base_q")
    try:
        yield c
    finally:
        sqlglot_lin.lineage = orig["lin_lineage"]
        base_mod.build_scope = orig["base_bs"]
        base_mod.qualify = orig["base_q"]


def assert_flat(base: int, doubled: int, label: str, slack: int = 2) -> None:
    """Assert a quantity that must stay constant did NOT grow when the input
    (column count, or schema-source count) doubled. The thing being measured is
    O(1) in that input by design, so it must stay constant up to a small slack."""
    assert doubled <= base + slack, (
        f"{label} grew from {base} to {doubled} when the input doubled, but this "
        f"quantity must be constant (once per statement, or file-level sources only). "
        f"A perf invariant regressed (see CLAUDE.md 'Performance invariants'). This is "
        f"the class of bug — O(N) flipping to O(N^2) — that turned 3-minute parses "
        f"into 40-minute parses."
    )


def assert_at_most_linear(
    base: int, doubled: int, label: str, factor: float = 2.5, slack: int = 2
) -> None:
    """Assert a per-column op grew at most ~linearly (≈2x) when columns doubled,
    not super-linearly."""
    ceiling = base * factor + slack
    assert doubled <= ceiling, (
        f"{label} grew from {base} to {doubled} when column count doubled — that is "
        f"super-linear (expected ≤ {ceiling:.0f} for linear growth). An O(N) op "
        f"became O(N^2)."
    )


def _wide_insert(n_cols: int) -> str:
    """A single INSERT...SELECT with n_cols pass-through columns. Statement count
    is always 1, so per-statement ops must stay flat as n_cols grows."""
    out_cols = ", ".join(f"c{i}" for i in range(n_cols))
    src_cols = ", ".join(f"src.c{i}" for i in range(n_cols))
    return f"INSERT INTO db.s.tgt ({out_cols}) SELECT {src_cols} FROM db.s.src;"


# ---------------------------------------------------------------------------
# Axis 1 — column count: per-statement ops must stay flat (parser hot path)
# ---------------------------------------------------------------------------


def test_per_statement_ops_flat_when_columns_double():
    """build_scope / qualify run once per statement, so doubling the column count
    of a single statement must not increase their call counts.

    Regression caught: per-column qualify (CLAUDE.md: 176 cols x 11 joins = 7 min).
    """
    parser_base = AnsiParser(SchemaResolver(dialect=None))
    parser_2x = AnsiParser(SchemaResolver(dialect=None))

    with count_hot_ops() as base:
        parser_base.parse_file(Path("base.sql"), _wide_insert(COLS_BASE))
    with count_hot_ops() as doubled:
        parser_2x.parse_file(Path("doubled.sql"), _wide_insert(COLS_2X))

    # Sanity: the column loop actually ran (sg_lineage is per-column).
    assert base.sg_lineage >= COLS_BASE // 2, (
        f"fixture did not exercise the column-lineage path (sg_lineage={base.sg_lineage}); "
        "the guard would be vacuously green"
    )

    assert_flat(base.build_scope, doubled.build_scope, "build_scope")
    assert_flat(base.qualify, doubled.qualify, "qualify")
    # sg_lineage is legitimately per-column; only bound its slope.
    assert_at_most_linear(base.sg_lineage, doubled.sg_lineage, "sg_lineage")
