"""Integration tests for the sprint-10 anchor tools (find_definition,
get_change_scope, get_backfill_order, diff_impact, scope_change).

Each test builds a real in-memory KuzuDB graph by indexing tiny SQL fixtures,
wires the module-level tools backend to it, and asserts on observable tool
output (specific names, counts, labels) — never just "no exception raised".
"""

import sqlcg.server.tools as tools
from sqlcg.core.kuzu_backend import KuzuBackend
from sqlcg.indexer.indexer import Indexer


def _index_fixture(tmp_path, files: dict[str, str], monkeypatch) -> KuzuBackend:
    """Write the given {filename: sql} fixtures, index them into a fresh
    in-memory graph, and wire it as the tools backend.

    chdir into tmp_path so NoiseFilter / presentation config resolve from a
    clean root (default patterns, no stray repo .sqlcg.toml).
    """
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)

    backend = KuzuBackend(":memory:")
    backend.init_schema()
    # _assert_indexed() requires a Repo node; the Indexer alone does not create one.
    backend.upsert_node("Repo", str(tmp_path), {"path": str(tmp_path), "name": tmp_path.name})
    Indexer().index_repo(tmp_path, dialect=None, db=backend)

    tools._backend = backend
    tools._metrics = None
    monkeypatch.chdir(tmp_path)
    return backend


# --------------------------------------------------------------------------
# PR-03 — find_definition
# --------------------------------------------------------------------------


def test_find_definition_single_authoritative(tmp_path, monkeypatch):
    """Scenario A — a single, non-backup definition is authoritative."""
    _index_fixture(
        tmp_path,
        {"dim_date.sql": "CREATE TABLE ba.dim_date (id INT);"},
        monkeypatch,
    )

    result = tools.find_definition("ba.dim_date")

    assert len(result.definitions) == 1, f"expected 1 definition, got {result.definitions}"
    assert result.definitions[0].is_authoritative is True
    assert result.definitions[0].is_backup is False
    assert result.duplicate_ddl is False
    assert result.definitions[0].file_path.endswith("dim_date.sql")


def test_find_definition_backup_flagged(tmp_path, monkeypatch):
    """Scenario B — a backup-named table is flagged is_backup, not hidden."""
    _index_fixture(
        tmp_path,
        {
            "dim_date.sql": "CREATE TABLE ba.dim_date (id INT);",
            "dim_date_bck.sql": "CREATE TABLE ba.dim_date_bck (id INT);",
        },
        monkeypatch,
    )

    result = tools.find_definition("ba.dim_date_bck")

    assert len(result.definitions) == 1
    assert result.definitions[0].is_backup is True, (
        "ba.dim_date_bck must match the default *_bck backup pattern"
    )
    assert result.definitions[0].is_authoritative is False
    assert any(p.endswith("dim_date_bck.sql") for p in result.noise_excluded)


def test_find_definition_duplicate_ddl(tmp_path, monkeypatch):
    """Scenario C — same table defined in two files is flagged duplicate_ddl."""
    _index_fixture(
        tmp_path,
        {
            "a.sql": "CREATE TABLE ba.dim_date (id INT);",
            "b.sql": "CREATE TABLE ba.dim_date (id INT, extra INT);",
        },
        monkeypatch,
    )

    result = tools.find_definition("ba.dim_date")

    assert result.duplicate_ddl is True
    assert len(result.definitions) == 2, f"expected 2 definitions, got {result.definitions}"


def test_find_definition_not_indexed(tmp_path, monkeypatch):
    """Scenario D — unknown table returns empty definitions and a hint."""
    _index_fixture(
        tmp_path,
        {"dim_date.sql": "CREATE TABLE ba.dim_date (id INT);"},
        monkeypatch,
    )

    result = tools.find_definition("ba.nonexistent_table")

    assert result.definitions == []
    assert result.hint is not None and len(result.hint) > 0
