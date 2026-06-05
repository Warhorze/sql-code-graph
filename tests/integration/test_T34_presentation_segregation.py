"""Failing acceptance tests for #34 — analyze_unused presentation/terminal segregation.

Each test builds a real in-memory KuzuDB graph by indexing tiny SQL fixtures,
wires the module-level tools backend to it, and asserts on the bucketed output
of tools.analyze_unused() — never just "no exception raised".

These tests MUST FAIL until the developer implements:
  - PresentationCandidate model in models.py
  - presentation_facing field on UnusedTablesResult
  - _matched_presentation_prefix helper in tools.py
  - prefix branch in analyze_unused loop in tools.py
  - updated hint logic in tools.py
  - updated skill.py _WORKFLOWS entry
"""

import pytest

import sqlcg.server.tools as tools
from sqlcg.core.duckdb_backend import DuckDBBackend
from sqlcg.indexer.indexer import Indexer

# PresentationCandidate is introduced by #34 — import with skip guard so the
# suite does not error before the developer lands the model.
try:
    from sqlcg.server.models import PresentationCandidate  # noqa: F401  # introduced by #34

    _PRESENTATION_CANDIDATE_AVAILABLE = True
except ImportError:
    _PRESENTATION_CANDIDATE_AVAILABLE = False


def _index_fixture(tmp_path, files: dict[str, str], monkeypatch) -> DuckDBBackend:
    """Write the given {filename: sql} fixtures, index them into a fresh
    in-memory graph, and wire it as the tools backend.

    chdir into tmp_path so NoiseFilter / presentation config resolve from a
    clean root (default patterns, no stray repo .sqlcg.toml).
    """
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)

    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend)

    tools._backend = backend
    tools._metrics = None
    monkeypatch.chdir(tmp_path)
    return backend


# ---------------------------------------------------------------------------
# Scenario A — presentation table segregated into presentation_facing bucket
# ---------------------------------------------------------------------------


def test_T34_scenario_A_presentation_table_segregated(tmp_path, monkeypatch):
    """Scenario A: with ia_ prefix configured, ia_pres.* orphan goes to
    presentation_facing, NOT candidates.  Non-prefixed orphan stays in candidates.
    """
    if not _PRESENTATION_CANDIDATE_AVAILABLE:
        pytest.skip("PresentationCandidate not yet implemented (#34)")

    _index_fixture(
        tmp_path,
        {
            ".sqlcg.toml": '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n',
            "pres.sql": "CREATE TABLE ia_pres.dashboard_feed (id INT);",
            "orphan.sql": "CREATE TABLE ba.real_orphan (id INT);",
        },
        monkeypatch,
    )

    result = tools.analyze_unused()

    pf_names = [e.table_qualified for e in result.presentation_facing]
    cand_names = [c.table_qualified for c in result.candidates]

    assert "ia_pres.dashboard_feed" in pf_names, (
        f"ia_pres.dashboard_feed must be in presentation_facing; got {pf_names}"
    )
    assert "ia_pres.dashboard_feed" not in cand_names, (
        f"ia_pres.dashboard_feed must NOT be in candidates; got {cand_names}"
    )
    assert "ba.real_orphan" in cand_names, f"ba.real_orphan must be in candidates; got {cand_names}"
    assert "ba.real_orphan" not in pf_names, (
        f"ba.real_orphan must NOT be in presentation_facing; got {pf_names}"
    )


# ---------------------------------------------------------------------------
# Scenario A (detail) — matched_prefix and reason are populated and correct
# ---------------------------------------------------------------------------


def test_T34_scenario_A_matched_prefix_and_reason(tmp_path, monkeypatch):
    """Scenario A detail: the presentation entry carries the matched prefix and
    a reason mentioning 'egress layer'.
    """
    if not _PRESENTATION_CANDIDATE_AVAILABLE:
        pytest.skip("PresentationCandidate not yet implemented (#34)")

    _index_fixture(
        tmp_path,
        {
            ".sqlcg.toml": '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n',
            "pres.sql": "CREATE TABLE ia_pres.dashboard_feed (id INT);",
        },
        monkeypatch,
    )

    result = tools.analyze_unused()

    assert len(result.presentation_facing) >= 1, (
        f"Expected at least one presentation_facing entry; got {result.presentation_facing}"
    )
    entry = next(
        e for e in result.presentation_facing if e.table_qualified == "ia_pres.dashboard_feed"
    )
    assert entry.matched_prefix == "ia_", (
        f"matched_prefix must be 'ia_'; got {entry.matched_prefix!r}"
    )
    assert "egress layer" in entry.reason, (
        f"reason must mention 'egress layer'; got {entry.reason!r}"
    )


# ---------------------------------------------------------------------------
# Scenario B — genuine orphan keeps its dead_code heuristic Judgement
# ---------------------------------------------------------------------------


def test_T34_scenario_B_genuine_orphan_stays_in_candidates(tmp_path, monkeypatch):
    """Scenario B: non-prefixed orphan keeps assertion_type='heuristic', confidence=0.5."""
    if not _PRESENTATION_CANDIDATE_AVAILABLE:
        pytest.skip("PresentationCandidate not yet implemented (#34)")

    _index_fixture(
        tmp_path,
        {
            ".sqlcg.toml": '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n',
            "pres.sql": "CREATE TABLE ia_pres.dashboard_feed (id INT);",
            "orphan.sql": "CREATE TABLE ba.real_orphan (id INT);",
        },
        monkeypatch,
    )

    result = tools.analyze_unused()

    orphan = next((c for c in result.candidates if c.table_qualified == "ba.real_orphan"), None)
    assert orphan is not None, (
        "ba.real_orphan must be in candidates; got "
        f"{[c.table_qualified for c in result.candidates]}"
    )
    assert orphan.dead_code.assertion_type == "heuristic", (
        f"assertion_type must be 'heuristic'; got {orphan.dead_code.assertion_type!r}"
    )
    assert orphan.dead_code.confidence == 0.5, (
        f"confidence must be 0.5; got {orphan.dead_code.confidence}"
    )


# ---------------------------------------------------------------------------
# Scenario C — all-presentation graph yields updated hint mentioning egress layer
# ---------------------------------------------------------------------------


def test_T34_scenario_C_all_presentation_hint(tmp_path, monkeypatch):
    """Scenario C: when all orphans are presentation-facing, candidates is empty
    and the hint describes the egress layer, NOT 'graph is empty'.
    """
    if not _PRESENTATION_CANDIDATE_AVAILABLE:
        pytest.skip("PresentationCandidate not yet implemented (#34)")

    _index_fixture(
        tmp_path,
        {
            ".sqlcg.toml": '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n',
            "pres1.sql": "CREATE TABLE ia_pres.feed_a (id INT);",
            "pres2.sql": "CREATE TABLE ia_pres.feed_b (id INT);",
        },
        monkeypatch,
    )

    result = tools.analyze_unused()

    assert result.candidates == [], (
        "candidates must be empty when all orphans are presentation-facing; "
        f"got {result.candidates}"
    )
    assert len(result.presentation_facing) >= 2, (
        f"presentation_facing must be non-empty; got {result.presentation_facing}"
    )
    assert result.hint is not None, "hint must be set when only presentation orphans exist"
    assert "egress" in result.hint.lower() or "presentation" in result.hint.lower(), (
        f"hint must mention egress/presentation; got {result.hint!r}"
    )
    # The old "graph is empty" message must NOT fire when there are presentation tables
    assert "graph is empty" not in result.hint, (
        f"hint must not say 'graph is empty' when presentation tables exist; got {result.hint!r}"
    )


# ---------------------------------------------------------------------------
# Scenario D — no config: small-repo path unchanged (regression guard)
# ---------------------------------------------------------------------------


def test_T34_scenario_D_no_config_byte_identical(tmp_path, monkeypatch):
    """Scenario D: with NO [sqlcg.presentation] configured, an ia_pres.* orphan
    lands in candidates (not presentation_facing) — proves no hardcoded prefix
    and that the small-repo experience is unchanged.
    """
    if not _PRESENTATION_CANDIDATE_AVAILABLE:
        pytest.skip("PresentationCandidate not yet implemented (#34)")

    _index_fixture(
        tmp_path,
        {
            # No .sqlcg.toml — bare directory
            "pres.sql": "CREATE TABLE ia_pres.dashboard_feed (id INT);",
        },
        monkeypatch,
    )

    result = tools.analyze_unused()

    assert result.presentation_facing == [], (
        f"presentation_facing must be empty when no prefix is configured; "
        f"got {result.presentation_facing}"
    )
    cand_names = [c.table_qualified for c in result.candidates]
    assert "ia_pres.dashboard_feed" in cand_names, (
        f"ia_pres.dashboard_feed must appear in candidates with no config; got {cand_names}"
    )


# ---------------------------------------------------------------------------
# Scenario E — noise wins over presentation prefix
# ---------------------------------------------------------------------------


def test_T34_scenario_E_noise_wins_over_presentation(tmp_path, monkeypatch):
    """Scenario E: a table matching BOTH a presentation prefix AND the *_bck noise
    glob appears in NEITHER bucket (noise filter runs first).
    """
    if not _PRESENTATION_CANDIDATE_AVAILABLE:
        pytest.skip("PresentationCandidate not yet implemented (#34)")

    _index_fixture(
        tmp_path,
        {
            ".sqlcg.toml": '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n',
            # table name is 'feed_bck' — matches *_bck glob AND ia_ prefix
            "bck.sql": "CREATE TABLE ia_pres.feed_bck (id INT);",
        },
        monkeypatch,
    )

    result = tools.analyze_unused()

    pf_names = [e.table_qualified for e in result.presentation_facing]
    cand_names = [c.table_qualified for c in result.candidates]

    assert "ia_pres.feed_bck" not in pf_names, (
        f"ia_pres.feed_bck must NOT appear in presentation_facing (noise wins); got {pf_names}"
    )
    assert "ia_pres.feed_bck" not in cand_names, (
        f"ia_pres.feed_bck must NOT appear in candidates (noise wins); got {cand_names}"
    )


# ---------------------------------------------------------------------------
# Wiring verification: _matched_presentation_prefix must have ≥2 hits in tools.py
# ---------------------------------------------------------------------------


def test_T34_wiring_helper_defined_and_called():
    """Verify _matched_presentation_prefix is defined AND called in tools.py
    (definition + at least one call site).

    This fails before implementation because the helper does not exist yet.
    """
    import importlib
    import inspect

    tools_mod = importlib.import_module("sqlcg.server.tools")
    assert hasattr(tools_mod, "_matched_presentation_prefix"), (
        "_matched_presentation_prefix must be defined in sqlcg.server.tools"
    )
    src = inspect.getsource(tools_mod)
    occurrences = src.count("_matched_presentation_prefix")
    assert occurrences >= 2, (
        f"_matched_presentation_prefix must appear ≥2 times in tools.py "
        f"(definition + call site); found {occurrences}"
    )


# ---------------------------------------------------------------------------
# Wiring verification: PresentationCandidate constructed in tools.py
# ---------------------------------------------------------------------------


def test_T34_wiring_presentation_candidate_constructed():
    """Verify PresentationCandidate(...) is instantiated in tools.py."""
    if not _PRESENTATION_CANDIDATE_AVAILABLE:
        pytest.skip("PresentationCandidate not yet implemented (#34)")

    import importlib
    import inspect

    tools_mod = importlib.import_module("sqlcg.server.tools")
    src = inspect.getsource(tools_mod)
    assert "PresentationCandidate(" in src, (
        "PresentationCandidate(...) must be constructed in sqlcg.server.tools"
    )
    assert "presentation_facing=" in src, (
        "presentation_facing= must appear in the UnusedTablesResult constructor in tools.py"
    )


# ---------------------------------------------------------------------------
# Skill doc — _WORKFLOWS mentions presentation_facing
# ---------------------------------------------------------------------------


def test_T34_skill_workflows_mentions_presentation_facing():
    """Step 4.1 AC: the analyze_unused workflow line in skill.py _WORKFLOWS mentions
    'presentation_facing' specifically in the analyze_unused context — not just in the
    diff_impact line that already carries it.

    Fails before Step 4.1 because the analyze_unused workflow bullet reads:
      'each dead_code is heuristic; table may be consumed externally'
    and makes no mention of the presentation_facing bucket.
    """
    from sqlcg.server.skill import _WORKFLOWS  # type: ignore[attr-defined]

    # Find the line(s) that describe analyze_unused in _WORKFLOWS and assert
    # that at least one of those lines mentions presentation_facing.
    lines = _WORKFLOWS.splitlines()
    analyze_lines = [line for line in lines if "analyze_unused" in line]
    assert analyze_lines, "_WORKFLOWS must contain an analyze_unused workflow entry"
    assert any("presentation_facing" in line for line in analyze_lines), (
        "The analyze_unused workflow line in _WORKFLOWS must mention 'presentation_facing' "
        "after Step 4.1; current analyze_unused line: " + repr(analyze_lines)
    )
