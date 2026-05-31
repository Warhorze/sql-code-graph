"""Failing acceptance tests for hygiene batch Phase 4 — MCP/CLI config-root reconciliation.

The bug under test: query-time MCP tools read presentation prefixes and the
NoiseFilter config from ``Path.cwd()`` instead of from the directory the graph was
actually indexed from. When the MCP server's working dir differs from the indexed
root, MCP computes a DIFFERENT presentation split / noise filter than what was
indexed — a silent disagreement between CLI (index-time) and MCP (query-time).

The fix (Phase 4): resolve the indexed root from the persisted ``Repo`` node
(``schema.cypher`` — ``path STRING PRIMARY KEY``, stored absolute/resolved) via a
``_indexed_root(db)`` helper and pass THAT to ``get_presentation_prefixes`` and the
MCP-side ``NoiseFilter.from_config(repo_root=...)`` — never ``Path.cwd()``.

Each test indexes a fixture from directory A (which carries ``[sqlcg.presentation]``),
then changes the process cwd to a DIFFERENT directory B (no ``.sqlcg.toml``) and
asserts the presentation split / noise filtering is STILL correct — resolved from the
Repo root, not from cwd. These assert OBSERVABLE tool output, never "no exception".

All tests MUST FAIL (red) before the developer lands Phase 4 because today
``analyze_unused`` / ``diff_impact`` call ``get_presentation_prefixes(Path.cwd())`` and
``_indexed_root`` does not exist.
"""

import importlib
import inspect

import pytest

import sqlcg.server.tools as tools
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


def _index_in_dir_A_then_chdir_B(tmp_path, files: dict[str, str], monkeypatch):
    """Write fixtures into dir A, index them (persisting a Repo node whose path is A),
    wire the backend into tools, then chdir to a SEPARATE dir B with no .sqlcg.toml.

    Returns (backend, dir_A, dir_B). The key property: after this call the process
    cwd is dir_B, so any tool that still reads Path.cwd() will see B (no config) and
    mis-split; a tool that reads the Repo root will see A (with config) and split right.
    """
    dir_a = tmp_path / "indexed_root"
    dir_b = tmp_path / "elsewhere"
    dir_a.mkdir()
    dir_b.mkdir()  # deliberately has NO .sqlcg.toml

    for name, sql in files.items():
        (dir_a / name).write_text(sql)

    backend = KuzuBackend(":memory:")
    backend.init_schema()
    # Repo PK is the indexed root path — exactly what index_cmd persists.
    backend.upsert_node("Repo", str(dir_a), {"path": str(dir_a), "name": dir_a.name})
    Indexer().index_repo(dir_a, dialect=None, db=backend)

    tools._backend = backend
    tools._metrics = None
    # The footgun trigger: cwd is now B, which has no config.
    monkeypatch.chdir(dir_b)
    return backend, dir_a, dir_b


# ---------------------------------------------------------------------------
# Scenario 1 — analyze_unused presentation split survives a cwd ≠ indexed-root
# ---------------------------------------------------------------------------


def test_reconciliation_analyze_unused_split_from_repo_root_not_cwd(tmp_path, monkeypatch):
    """analyze_unused must resolve presentation prefixes from the indexed Repo root.

    Index dir A with [sqlcg.presentation] schema_prefixes=["ia_"]; an ia_pres.* orphan
    must land in presentation_facing. With cwd pointed at config-less dir B, today's
    get_presentation_prefixes(Path.cwd()) returns [] and the table wrongly falls into
    candidates — this assertion fails until Phase 4 resolves the root from the Repo node.
    """
    _index_in_dir_A_then_chdir_B(
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
        "presentation prefix must resolve from the indexed Repo root even when cwd is a "
        f"different, config-less directory; got presentation_facing={pf_names}, "
        f"candidates={cand_names}"
    )
    assert "ia_pres.dashboard_feed" not in cand_names, (
        f"ia_pres.dashboard_feed must NOT fall into candidates; got {cand_names}"
    )
    # The genuine, non-prefixed orphan is unaffected and stays a candidate.
    assert "ba.real_orphan" in cand_names, f"ba.real_orphan must stay a candidate; got {cand_names}"


# ---------------------------------------------------------------------------
# Scenario 2 — diff_impact presentation flagging survives cwd ≠ indexed-root
# ---------------------------------------------------------------------------


def test_reconciliation_diff_impact_presentation_from_repo_root_not_cwd(tmp_path, monkeypatch):
    """diff_impact must flag presentation-facing tables using the indexed Repo root's
    prefixes, not cwd's. With cwd on config-less dir B, the changed presentation table
    must still be flagged presentation-facing.
    """
    _index_in_dir_A_then_chdir_B(
        tmp_path,
        {
            ".sqlcg.toml": '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n',
            "pres.sql": "CREATE TABLE ia_pres.dashboard_feed AS SELECT id FROM ba.src;",
            "src.sql": "CREATE TABLE ba.src (id INT);",
        },
        monkeypatch,
    )

    result = tools.diff_impact(["src.sql"])

    # The presentation subset of the blast radius must include the ia_ table because the
    # prefix resolved from dir A's config, not from config-less cwd dir B.
    pres_names = {getattr(p, "table_qualified", p) for p in result.presentation}
    assert any("ia_pres.dashboard_feed" == str(n) for n in pres_names), (
        "diff_impact must flag ia_pres.dashboard_feed as presentation-facing using the "
        f"indexed Repo root's prefixes; got presentation={pres_names!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — no Repo node → fall back to cwd, never crash
# ---------------------------------------------------------------------------


def test_reconciliation_no_repo_node_falls_back_to_cwd(tmp_path, monkeypatch):
    """_indexed_root must return None (and callers fall back to Path.cwd()) when no Repo
    node exists, never raise. analyze_unused must still run on a tables-only graph.
    """
    # _indexed_root is introduced by Phase 4; skip-guard so the suite collects cleanly.
    indexed_root = getattr(tools, "_indexed_root", None)
    if indexed_root is None:
        pytest.skip("_indexed_root not yet implemented (Phase 4)")

    backend = KuzuBackend(":memory:")
    backend.init_schema()
    # NOTE: deliberately NO Repo node upserted.
    (tmp_path / "orphan.sql").write_text("CREATE TABLE ba.real_orphan (id INT);")
    Indexer().index_repo(tmp_path, dialect=None, db=backend)
    tools._backend = backend
    tools._metrics = None
    monkeypatch.chdir(tmp_path)

    assert indexed_root(backend) is None, "_indexed_root must return None when no Repo node exists"
    # Must not crash — analyze_unused falls back to cwd behaviour.
    result = tools.analyze_unused()
    cand_names = [c.table_qualified for c in result.candidates]
    assert "ba.real_orphan" in cand_names, (
        f"with no Repo node, analyze_unused must still run via cwd fallback; got {cand_names}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — _indexed_root returns the persisted Repo path
# ---------------------------------------------------------------------------


def test_reconciliation_indexed_root_returns_repo_path(tmp_path, monkeypatch):
    """_indexed_root(db) returns the absolute path stored on the Repo node."""
    indexed_root = getattr(tools, "_indexed_root", None)
    if indexed_root is None:
        pytest.skip("_indexed_root not yet implemented (Phase 4)")

    _backend, dir_a, _dir_b = _index_in_dir_A_then_chdir_B(
        tmp_path,
        {
            ".sqlcg.toml": '[sqlcg.presentation]\nschema_prefixes = ["ia_"]\n',
            "pres.sql": "CREATE TABLE ia_pres.dashboard_feed (id INT);",
        },
        monkeypatch,
    )

    from pathlib import Path

    resolved = indexed_root(tools._backend)
    assert resolved == Path(str(dir_a)), (
        f"_indexed_root must return the Repo node path {dir_a!r}; got {resolved!r}"
    )


# ---------------------------------------------------------------------------
# Wiring guard — _indexed_root defined AND called; no Path.cwd() in the two
# presentation call sites after Phase 4.
# ---------------------------------------------------------------------------


def test_reconciliation_wiring_helper_defined_and_called():
    """_indexed_root must be defined in tools.py and called (definition + ≥1 call site)."""
    tools_mod = importlib.import_module("sqlcg.server.tools")
    assert hasattr(tools_mod, "_indexed_root"), (
        "_indexed_root must be defined in sqlcg.server.tools (Phase 4)"
    )
    src = inspect.getsource(tools_mod)
    assert src.count("_indexed_root") >= 2, (
        "_indexed_root must appear ≥2 times in tools.py (definition + call site); "
        f"found {src.count('_indexed_root')}"
    )


def test_reconciliation_wiring_presentation_no_longer_reads_cwd():
    """After Phase 4, get_presentation_prefixes must NOT be called with Path.cwd().

    Fails today because both analyze_unused and diff_impact call
    get_presentation_prefixes(Path.cwd()).
    """
    tools_mod = importlib.import_module("sqlcg.server.tools")
    src = inspect.getsource(tools_mod)
    assert "get_presentation_prefixes(Path.cwd())" not in src, (
        "get_presentation_prefixes(Path.cwd()) must be replaced by a Repo-root-resolved "
        "path in Phase 4 (resolved via _indexed_root with a cwd fallback)"
    )
