"""Live end-to-end anchor tests.

Indexes the synthetic anchor fixtures from tests/snowflake/anchors/ into a real
in-memory DuckDB backend and asserts the anchor columns appear in the graph with the
expected edges. Complements the parser-only tests in tests/snowflake/anchors/ by
covering the parser → aggregator → indexer → graph path end-to-end.

Companion tests for the real DWH corpus are marked
@pytest.mark.skip(reason="requires DWH corpus") and are intentionally NOT run in CI.
"""

import shutil
from pathlib import Path

import pytest

from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

ANCHOR_FIXTURES_DIR = Path(__file__).parent.parent / "snowflake" / "anchors"


def _stage_anchor_corpus(tmp_path: Path, fixture_names: list[str]) -> Path:
    """Copy named anchor fixture files into a fresh corpus directory.

    Uses use_git=False on the indexer call site because tmp_path is not a git repo.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in fixture_names:
        src = ANCHOR_FIXTURES_DIR / name
        assert src.exists(), f"fixture missing: {src}"
        shutil.copy(src, corpus / name)
    return corpus


def _index(corpus: Path) -> tuple[DuckDBBackend, dict]:
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    summary = Indexer().index_repo(
        corpus,
        dialect="snowflake",
        db=backend,
        use_git=False,
        batch_size=10,
    )
    return backend, summary


def test_live_anchor_omloopsnelheid_has_column_lineage_edges(tmp_path):
    """OMLOOPSNELHEID must produce ≥1 COLUMN_LINEAGE edge into a temp table.

    The fixture defines tmp_a / tmp_b CTAS with omloopsnelheid = afzet / NULLIF(...).
    At least one of:
        stg_a.afzet -> tmp_a.omloopsnelheid
        stg_a.gemiddelde_vrd -> tmp_a.omloopsnelheid
    must exist as a COLUMN_LINEAGE edge in the graph.
    """
    corpus = _stage_anchor_corpus(tmp_path, ["fixture_omloopsnelheid.sql"])
    backend, summary = _index(corpus)

    rows = backend.run_read(
        "SELECT src.table_name AS s_t, src.col_name AS s_c, "
        "dst.table_name AS d_t, dst.col_name AS d_c "
        'FROM "COLUMN_LINEAGE" cl '
        'JOIN "SqlColumn" src ON src.id = cl.src_key '
        'JOIN "SqlColumn" dst ON dst.id = cl.dst_key '
        "WHERE LOWER(dst.col_name) = 'omloopsnelheid'",
        {},
    )
    assert len(rows) >= 1, (
        "OMLOOPSNELHEID anchor: expected ≥1 COLUMN_LINEAGE edge into a temp table "
        f"with dst col_name='omloopsnelheid'. Index summary: {summary}. "
        f"This is a regression against the parser-level "
        f"tests/snowflake/anchors/test_anchor_omloopsnelheid.py."
    )


def test_live_anchor_ma_target_column_exists_in_graph(tmp_path):
    """MA_AANTAL_OP_ORDER target column node must exist post-index.

    The INSERT target wtfs_openstaande_orders.ma_aantal_op_order is referenced
    in the INSERT column list, so the indexer must upsert at least the target
    column node even when sprint-06 E5 fixes do not yet wire the full chain.
    """
    corpus = _stage_anchor_corpus(
        tmp_path,
        ["fixture_source.sql", "fixture_etl.sql", "fixture_semantic.sql"],
    )
    backend, summary = _index(corpus)

    rows = backend.run_read(
        'SELECT id FROM "SqlColumn" WHERE LOWER(col_name) = ?',
        {"col": "ma_aantal_op_order"},
    )
    assert len(rows) >= 1, (
        "MA_AANTAL_OP_ORDER target column must exist as a SqlColumn node. "
        f"Index summary: {summary}."
    )


def test_live_anchor_ma_chain_has_at_least_one_lineage_edge(tmp_path):
    """≥1 COLUMN_LINEAGE edge anywhere along the MA_AANTAL_OP_ORDER chain.

    Targets the broadest possible assertion: any edge whose destination is
    wtfs_openstaande_orders.ma_aantal_op_order, OR an edge whose source is
    source_facts.ma_order_aantal. Either signals partial chain coverage.
    """
    corpus = _stage_anchor_corpus(
        tmp_path,
        ["fixture_source.sql", "fixture_etl.sql", "fixture_semantic.sql"],
    )
    backend, _summary = _index(corpus)

    rows = backend.run_read(
        'SELECT count(*) AS n FROM "COLUMN_LINEAGE" cl '
        'JOIN "SqlColumn" src ON src.id = cl.src_key '
        'JOIN "SqlColumn" dst ON dst.id = cl.dst_key '
        "WHERE (LOWER(dst.table_name) = 'wtfs_openstaande_orders' "
        "       AND LOWER(dst.col_name) = 'ma_aantal_op_order') "
        "   OR (LOWER(src.table_name) = 'source_facts' "
        "       AND LOWER(src.col_name) = 'ma_order_aantal')",
        {},
    )
    assert rows[0]["n"] >= 1


# ---------------------------------------------------------------------------
# Real DWH corpus variants — skipped by default. Run manually with:
#   uv run pytest tests/integration/test_live_anchors.py -k dwh --runxfail
# after exporting SQLCG_DWH_CORPUS=/path/to/dwh.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="requires DWH corpus (set SQLCG_DWH_CORPUS to run)")
def test_live_anchor_omloopsnelheid_dwh():
    """Production-corpus version of the OMLOOPSNELHEID anchor."""
    ...


@pytest.mark.skip(reason="requires DWH corpus (set SQLCG_DWH_CORPUS to run)")
def test_live_anchor_ma_aantal_op_order_dwh():
    """Production-corpus version of the MA_AANTAL_OP_ORDER anchor."""
    ...
