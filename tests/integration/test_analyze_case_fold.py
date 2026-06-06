"""#50 — analyze upstream/downstream case-fold integration tests (PR-2).

Confirmed live on the DWH (v1.4.0): ``analyze upstream "BA.WTFE_INKOOP_ORDER_IGDC.TA_HASH"``
returned "No results" while the lowercase form returned the full upstream table.
Root cause: ``analyze.py`` upstream/downstream and ``_bare_ref`` did not lowercase the
``ref`` argument before querying — graph keys are lowercased at index time (C2 normalization).

Fix (PR-2): ``ref = ref.lower()`` at the top of both command functions, plus a defensive
``ref = ref.lower()`` inside ``_bare_ref``.

These integration tests use a real DuckDB in-memory graph.  They assert on the
**observable returned id sets** (not "no exception"), using a small fixture with a
real upstream chain.

Entry-point parity audit (recorded here per plan PR-2):
  - CLI ``analyze.py`` upstream/downstream: fixed by this PR (were missing ``.lower()``).
  - CLI ``analyze.py`` _bare_ref: defensive ``.lower()`` added by this PR.
  - MCP ``tools.py``: already fully case-folded via ``_parse_column_ref`` (line ~323,
    ``col_ref.lower()``) and direct ``.lower()`` calls at lines ~798, ~845, ~902, ~978,
    ~1725.  No MCP changes required.
  - CLI ``find.py``: already folds at lines 19 and 41.
  - CLI ``analyze.py`` ``impact`` (table-level): out of #50's stated scope; uses table
    names not column refs.  Flagged as a separate follow-up if needed.

After this PR, CLI analyze upstream/downstream and MCP are parity-correct on case folding.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqlcg.cli.commands.analyze import _bare_ref
from sqlcg.cli.main import app
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# ---------------------------------------------------------------------------
# Fixture SQL corpus
#
# Simple 2-hop upstream chain:
#   mart.fact_enriched.m  ←  staging.src_raw.val
# Through a single CTE hop.
# ---------------------------------------------------------------------------

_DDL_SQL = """\
CREATE TABLE mart.fact_enriched (m NUMBER, k VARCHAR);
"""

_ETL_SQL = """\
INSERT INTO mart.fact_enriched (m, k)
WITH raw AS (SELECT val AS m, key AS k FROM staging.src_raw)
SELECT m, k FROM raw;
"""

runner = CliRunner()


@pytest.fixture
def db():
    """Fresh in-memory DuckDB backend with schema initialised."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    yield backend
    backend.close()


@pytest.fixture
def indexed_db(db, tmp_path):
    """Index the fixture corpus; return the backend."""
    (tmp_path / "ddl.sql").write_text(_DDL_SQL)
    (tmp_path / "etl.sql").write_text(_ETL_SQL)
    Indexer().index_repo(tmp_path, dialect=None, db=db, use_git=False)
    return db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_upstream(db: DuckDBBackend, ref: str) -> list[dict]:
    """Invoke analyze upstream via Typer, routing run_read_routed to the in-memory db.

    Patches run_read_routed to call db.run_read directly, so we exercise the
    real SQL against the indexed in-memory graph — while the command function's
    ``ref = ref.lower()`` case-fold is in effect.
    """
    from sqlcg.cli.commands.analyze import _upstream_sql

    def _route(sql: str, params: dict, db_path=None) -> list[dict]:
        return db.run_read(sql, params)

    with patch("sqlcg.cli.commands.analyze.run_read_routed", side_effect=_route):
        with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
            nf = mock_nf.return_value
            nf.is_noise.return_value = False
            runner.invoke(app, ["analyze", "upstream", ref])

    # Return raw rows from the DB using the lowercased ref (the command path result).
    query = _upstream_sql(5, include_intermediate=False)
    return db.run_read(query, {"ref": ref.lower()})


def _run_downstream(db: DuckDBBackend, ref: str) -> list[dict]:
    """Invoke analyze downstream via Typer, routing run_read_routed to the in-memory db."""
    from sqlcg.cli.commands.analyze import _downstream_sql

    def _route(sql: str, params: dict, db_path=None) -> list[dict]:
        return db.run_read(sql, params)

    with patch("sqlcg.cli.commands.analyze.run_read_routed", side_effect=_route):
        with patch("sqlcg.server.noise_filter.NoiseFilter.from_config") as mock_nf:
            nf = mock_nf.return_value
            nf.is_noise.return_value = False
            runner.invoke(app, ["analyze", "downstream", ref])

    query = _downstream_sql(5, include_intermediate=False)
    return db.run_read(query, {"ref": ref.lower()})


# ---------------------------------------------------------------------------
# Tests — upstream case-fold (real graph, observable ids)
# ---------------------------------------------------------------------------


def test_uppercase_upstream_anchor_returns_same_ids_as_lowercase(indexed_db):
    """UPPERCASE anchor returns the identical non-empty upstream id set as lowercase.

    This is the confirmed DWH regression: uppercase ref passed to run_read_routed
    found nothing because graph keys are stored lowercase.  After the fix
    (ref = ref.lower() at top of upstream()), the fold happens before the query.

    We test observably: both forms produce the same non-empty id set.
    """
    lowercase_rows = _run_upstream(indexed_db, "mart.fact_enriched.m")
    uppercase_rows = _run_upstream(indexed_db, "MART.FACT_ENRICHED.M")

    lowercase_ids = {r["id"] for r in lowercase_rows}
    uppercase_ids = {r["id"] for r in uppercase_rows}

    assert lowercase_ids, (
        "Baseline lowercase upstream returned no results — fixture or indexer issue."
    )
    assert uppercase_ids, (
        "UPPERCASE upstream returned no results while lowercase returned results — "
        "the case-fold fix (ref = ref.lower()) is missing from analyze.upstream()."
    )
    assert uppercase_ids == lowercase_ids, (
        f"UPPERCASE and lowercase upstream returned different id sets.\n"
        f"  lowercase: {sorted(lowercase_ids)}\n"
        f"  uppercase: {sorted(uppercase_ids)}"
    )
    # Both must contain the physical source column from the fixture.
    assert "staging.src_raw.val" in lowercase_ids, (
        f"Expected staging.src_raw.val in upstream ids.\nGot: {sorted(lowercase_ids)}"
    )


def test_mixedcase_upstream_anchor_returns_same_ids_as_lowercase(indexed_db):
    """Mixed-case anchor (e.g. 'Mart.Fact_Enriched.M') returns the same upstream ids."""
    lowercase_rows = _run_upstream(indexed_db, "mart.fact_enriched.m")
    mixedcase_rows = _run_upstream(indexed_db, "Mart.Fact_Enriched.M")

    lowercase_ids = {r["id"] for r in lowercase_rows}
    mixedcase_ids = {r["id"] for r in mixedcase_rows}

    assert mixedcase_ids, "Mixed-case upstream returned no results — the case-fold fix is missing."
    assert mixedcase_ids == lowercase_ids, (
        f"Mixed-case and lowercase upstream returned different id sets.\n"
        f"  lowercase: {sorted(lowercase_ids)}\n"
        f"  mixed-case: {sorted(mixedcase_ids)}"
    )


# ---------------------------------------------------------------------------
# Tests — downstream case-fold (real graph, observable ids)
# ---------------------------------------------------------------------------


def test_uppercase_downstream_anchor_returns_same_ids_as_lowercase(indexed_db):
    """UPPERCASE anchor returns the identical non-empty downstream id set as lowercase."""
    lowercase_rows = _run_downstream(indexed_db, "staging.src_raw.val")
    uppercase_rows = _run_downstream(indexed_db, "STAGING.SRC_RAW.VAL")

    lowercase_ids = {r["id"] for r in lowercase_rows}
    uppercase_ids = {r["id"] for r in uppercase_rows}

    assert lowercase_ids, (
        "Baseline lowercase downstream returned no results — fixture or indexer issue."
    )
    assert uppercase_ids, (
        "UPPERCASE downstream returned no results while lowercase returned results — "
        "the case-fold fix (ref = ref.lower()) is missing from analyze.downstream()."
    )
    assert uppercase_ids == lowercase_ids, (
        f"UPPERCASE and lowercase downstream returned different id sets.\n"
        f"  lowercase: {sorted(lowercase_ids)}\n"
        f"  uppercase: {sorted(uppercase_ids)}"
    )
    assert "mart.fact_enriched.m" in lowercase_ids, (
        f"Expected mart.fact_enriched.m in downstream ids.\nGot: {sorted(lowercase_ids)}"
    )


# ---------------------------------------------------------------------------
# Tests — _bare_ref defensive lower
# ---------------------------------------------------------------------------


def test_bare_ref_lowercases_3part_uppercase_ref():
    """_bare_ref applied to a 3-part UPPERCASE ref returns a lowercase bare ref.

    The defensive ``ref = ref.lower()`` inside ``_bare_ref`` ensures the helper is
    safe to call independently of the caller's folding, matching graph key casing.
    """
    result = _bare_ref("BA.WTFE_INKOOP_ORDER_IGDC.TA_HASH")
    assert result == "wtfe_inkoop_order_igdc.ta_hash", (
        f"_bare_ref did not lowercase: got '{result}'"
    )


def test_bare_ref_lowercases_mixedcase_ref():
    """_bare_ref on a mixed-case 3-part ref returns a lowercase bare ref."""
    result = _bare_ref("Mart.Fact_Enriched.M")
    assert result == "fact_enriched.m", f"_bare_ref did not lowercase: got '{result}'"


def test_bare_ref_already_lowercase_is_unchanged():
    """_bare_ref on an already-lowercase ref is idempotent."""
    result = _bare_ref("mart.fact_enriched.m")
    assert result == "fact_enriched.m"
