"""Regression test for Bug B — MCP index_repo escalation must use the path
init_backend() received, not str(get_db_path()).

Live-DWH test (#29): pytest called init_backend(str(tmp_path / "test.db"))
then index_repo(), but escalation re-called str(get_db_path()) → opened the
user's real ~/.sqlcg/graph.db, wrote fixtures into it, held the live DB lock.

Fix: add _init_db_path module global, capture it in init_backend, use
_escalation_db_path() accessor in index_repo instead of get_db_path().

This test uses monkeypatch to make get_db_path() raise during the escalation
window, proving that the post-fix path does NOT call get_db_path() for
escalation. Before the fix, the test fails because get_db_path() IS called.
"""

from __future__ import annotations

from pathlib import Path


def test_BugB_escalation_does_not_call_get_db_path(tmp_path: Path, monkeypatch) -> None:
    """index_repo escalation must use the path init_backend() received.

    After init_backend(tmp_db), get_db_path() must NOT be called during the
    index_repo escalation window (tools.py:_get_or_escalate_rw / _de_escalate).

    Observable: no RuntimeError raised from the sentinel; index completes and
    returns a dict with files_parsed >= 0. The sentinel raises on ANY call,
    which proves the fix routes through _init_db_path, not get_db_path().

    Before the fix: get_db_path() IS called at tools.py:579 → sentinel raises.
    After the fix: _escalation_db_path() uses _init_db_path → sentinel never called.
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
            "get_db_path() must NOT be called during index_repo escalation. "
            "Bug B: escalation is re-fetching the default path instead of using "
            "the path init_backend() received."
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


def test_BugB_default_path_not_written(tmp_path: Path) -> None:
    """The real ~/.sqlcg/graph.db must not be touched during index_repo.

    Complementary observable check: record mtime / existence of the default DB
    before and after; assert it is unchanged.

    Before the fix: escalation opens the real DB and may write to it.
    After the fix: escalation uses the tmp_path DB exclusively.
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
        "Bug B: index_repo wrote to ~/.sqlcg/graph.db instead of the tmp_path DB."
    )
    if existed_before and existed_after:
        assert mtime_before == mtime_after, (
            f"Default DB mtime changed from {mtime_before} to {mtime_after}. "
            "Bug B: index_repo wrote to the real ~/.sqlcg/graph.db."
        )
