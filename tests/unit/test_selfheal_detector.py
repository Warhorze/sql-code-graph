"""Unit tests for the version-skew detector in src/sqlcg/server/selfheal.py.

Covers T1–T3 from plan/sprints/mcp_server_self_healing.md §Test Strategy (PR 1):

- T1: read_ondisk_version returns a non-None version string in a clean test environment.
- T2: read_ondisk_version returns None when distributions() yields nothing (F1 guard).
- T3: detect_skew returns (True, running, ondisk) on a mismatch and (False, …) on a match
      or when ondisk is None.

Also covers:
- cache-invalidation: the invalidate_caches() call is made before the distributions() scan.

Guards plan/sprints/mcp_server_self_healing.md §D1 / §Step 1.3.
"""

from __future__ import annotations

import importlib.metadata as im
from unittest.mock import MagicMock, patch

from sqlcg import __version__
from sqlcg.server.selfheal import detect_skew, read_ondisk_version

# ---------------------------------------------------------------------------
# T1 — read_ondisk_version returns the installed distribution version
# ---------------------------------------------------------------------------


def test_read_ondisk_version_returns_installed_version():
    """read_ondisk_version returns the same version as importlib.metadata in the test env.

    In a clean test environment (the package is installed in the uv venv),
    read_ondisk_version() must return a non-None version string equal to the
    distribution metadata.  Guards plan/sprints/mcp_server_self_healing.md T1.
    """
    result = read_ondisk_version()
    assert result is not None, "read_ondisk_version returned None in a live test env"
    # Must equal the installed distribution version (not necessarily __version__ —
    # after the bump they should match, but the key invariant is it is non-None and
    # equals what importlib.metadata reports for the installed dist)
    expected = im.version("sql-code-graph")
    assert result == expected, f"Expected {expected!r}, got {result!r}"


# ---------------------------------------------------------------------------
# T2 — read_ondisk_version returns None when distribution is absent
# ---------------------------------------------------------------------------


def test_read_ondisk_version_returns_none_when_dist_absent():
    """read_ondisk_version returns None when distributions() yields nothing.

    Simulates an environment where the package is not installed (e.g. a bare
    Python that somehow runs the code).  Must return None, not raise.
    Guards plan/sprints/mcp_server_self_healing.md T2 / F1.
    """
    with patch.object(im, "distributions", return_value=iter([])):
        result = read_ondisk_version()
    assert result is None


def test_read_ondisk_version_returns_none_on_exception():
    """read_ondisk_version returns None when distributions() raises.

    Guards the broad except in read_ondisk_version — any unexpected error
    from the metadata scan must never propagate to the caller.
    Guards plan/sprints/mcp_server_self_healing.md §F1.
    """
    with patch.object(im, "distributions", side_effect=RuntimeError("boom")):
        result = read_ondisk_version()
    assert result is None


def test_read_ondisk_version_returns_none_when_name_absent():
    """read_ondisk_version returns None when dist.metadata['Name'] is None/empty.

    Distributions without a Name field are skipped; if none match 'sql-code-graph',
    None is returned.  Guards plan/sprints/mcp_server_self_healing.md T2.
    """
    mock_dist = MagicMock()
    mock_dist.metadata.__getitem__ = lambda self, key: None  # Name returns None

    with patch.object(im, "distributions", return_value=iter([mock_dist])):
        result = read_ondisk_version()
    assert result is None


# ---------------------------------------------------------------------------
# Cache-invalidation: invalidate_caches() is called before the scan
# ---------------------------------------------------------------------------


def test_read_ondisk_version_calls_invalidate_caches_before_scan():
    """invalidate_caches() is called before distributions() on every invocation.

    This ensures a same-mtime .dist-info replacement is still observed (D1, A1).
    Guards plan/sprints/mcp_server_self_healing.md §D1.
    """
    call_order: list[str] = []

    def fake_invalidate():
        call_order.append("invalidate")

    def fake_distributions():
        call_order.append("distributions")
        return iter([])

    with (
        patch.object(im.MetadataPathFinder, "invalidate_caches", fake_invalidate),
        patch.object(im, "distributions", fake_distributions),
    ):
        read_ondisk_version()

    assert call_order == ["invalidate", "distributions"], (
        f"Expected invalidate before distributions; got: {call_order}"
    )


# ---------------------------------------------------------------------------
# T3 — detect_skew behaviour
# ---------------------------------------------------------------------------


def test_detect_skew_returns_true_when_ondisk_differs():
    """detect_skew returns (True, running, ondisk) when on-disk version differs.

    Guards plan/sprints/mcp_server_self_healing.md T3.
    """
    different_version = "99.0.0-new"
    with patch("sqlcg.server.selfheal.read_ondisk_version", return_value=different_version):
        skew, running, ondisk = detect_skew()

    assert skew is True, f"Expected skew=True; got {skew}"
    assert running == __version__, f"running should be baked-in __version__; got {running!r}"
    assert ondisk == different_version, f"ondisk should be the patched value; got {ondisk!r}"


def test_detect_skew_returns_false_when_ondisk_matches():
    """detect_skew returns (False, running, ondisk) when on-disk matches the running version.

    Guards plan/sprints/mcp_server_self_healing.md T3.
    """
    with patch("sqlcg.server.selfheal.read_ondisk_version", return_value=__version__):
        skew, running, ondisk = detect_skew()

    assert skew is False, f"Expected skew=False; got {skew}"
    assert running == __version__
    assert ondisk == __version__


def test_detect_skew_returns_false_when_ondisk_is_none():
    """detect_skew returns (False, …) when on-disk version is None (F1 guard).

    Never treat an unreadable on-disk distribution as a version skew.
    Guards plan/sprints/mcp_server_self_healing.md T3 / F1.
    """
    with patch("sqlcg.server.selfheal.read_ondisk_version", return_value=None):
        skew, running, ondisk = detect_skew()

    assert skew is False, f"Expected skew=False when ondisk is None; got {skew}"
    assert running == __version__
    assert ondisk is None
