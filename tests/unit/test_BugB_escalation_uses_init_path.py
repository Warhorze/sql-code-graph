"""Regression test for Bug B — index_repo must use the path init_backend() received.

After the Phase 4 DuckDB migration, the RO→RW escalation machinery was deleted
(no mode-switching needed — DuckDB holds a single R/W handle).  The bug was that
escalation re-called str(get_db_path()) → opened the user's real ~/.sqlcg/graph.db.
The fix captures _init_db_path in init_backend.

With the escalation removed, index_repo uses _get_backend() directly and never
re-opens any path.  These tests confirm the init_backend path is used (i.e. the
default DB is untouched) and that the tool returns valid output.
"""

from __future__ import annotations

from pathlib import Path


def test_index_repo_does_not_call_get_db_path(tmp_path: Path, monkeypatch) -> None:
    """index_repo must use the backend init_backend() opened, not get_db_path().

    Observable: no AssertionError raised from the sentinel; index completes and
    returns a dict with files_parsed >= 0.
    """
    import sqlcg.server.tools as _tools
    from sqlcg.server.tools import index_repo, init_backend, shutdown_backend

    tmp_db = str(tmp_path / "test.db")
    fixture_path = str(Path(__file__).parent.parent / "fixtures" / "synthetic")

    # Sentinel: raises if get_db_path() is called after init_backend has been given
    # an explicit path.  init_backend uses `db_path or str(get_db_path())` — since
    # we pass a path (truthy), the `or` branch is never taken, so the sentinel is
    # safe for the entire duration of this test.
    def _raising_get_db_path():
        raise AssertionError(
            "get_db_path() must NOT be called during index_repo. "
            "index_repo should use the backend init_backend() opened."
        )

    monkeypatch.setattr(_tools, "get_db_path", _raising_get_db_path)

    try:
        init_backend(tmp_db)
        result = index_repo(fixture_path)

        # Observable: index returned a non-empty result dict, not an exception
        assert isinstance(result, dict), f"Expected dict result, got {type(result)}"
        assert "files_parsed" in result, f"Result missing files_parsed: {result}"
    finally:
        shutdown_backend()  # Reset _init_db_path global between tests


def test_default_path_not_written(tmp_path: Path) -> None:
    """The real ~/.sqlcg/graph.db must not be touched during index_repo.

    Observable: mtime / existence of the default DB unchanged before and after.
    """
    from sqlcg.core.config import get_db_path
    from sqlcg.server.tools import index_repo, init_backend, shutdown_backend

    tmp_db = str(tmp_path / "test.db")
    fixture_path = str(Path(__file__).parent.parent / "fixtures" / "synthetic")

    default_db = get_db_path()
    mtime_before = default_db.stat().st_mtime if default_db.exists() else None
    existed_before = default_db.exists()

    try:
        init_backend(tmp_db)
        index_repo(fixture_path)
    finally:
        shutdown_backend()

    # After the call: the default DB must be in the exact same state as before
    existed_after = default_db.exists()
    mtime_after = default_db.stat().st_mtime if default_db.exists() else None

    assert existed_before == existed_after, (
        f"Default DB existence changed: was {existed_before}, now {existed_after}. "
        "index_repo wrote to ~/.sqlcg/graph.db instead of the tmp_path DB."
    )
    if existed_before and existed_after:
        assert mtime_before == mtime_after, (
            f"Default DB mtime changed from {mtime_before} to {mtime_after}. "
            "index_repo wrote to the real ~/.sqlcg/graph.db."
        )
