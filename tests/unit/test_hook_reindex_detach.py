"""Tests for #77 — hook reindex (--notify, no server) detaches in the background.

Before this fix, `sqlcg reindex --notify ...` (the post-checkout/post-merge hook
command) fell through to the direct synchronous resync path when no MCP server
was live, blocking `git checkout`/`git merge` for the full resync time.

This module guards:
  - hook path + no server: returns promptly, prints exactly one background
    notice, spawns a detached `subprocess.Popen` with `start_new_session=True`,
    `SQLCG_HOOK_DETACHED=1` set, and a log file derived from `get_db_path()`.
  - the respawn guard: with `SQLCG_HOOK_DETACHED=1` already set, no re-spawn
    occurs and the direct synchronous path runs in-process.
  - manual reindex (no `--notify`) never detaches.
  - the routed path (server live) never spawns.
  - the detached child's bounded lock-retry/backoff and failure logging.

See issue #77 (https://github.com/Warhorze/sql-code-graph/issues/77).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest
from typer.testing import CliRunner

from sqlcg.cli.commands import reindex as reindex_module
from sqlcg.cli.main import app

runner = CliRunner()


class _DetachPopenSpy:
    """Records calls that look like the #77 detach spawn, passes everything else through.

    ``reindex_module.subprocess`` is the real ``subprocess`` module, and
    ``_resolve_ref``/``_get_head`` use ``subprocess.run`` (which itself calls
    ``Popen`` internally) — a blanket ``Popen`` mock would break those git
    calls. This spy only intercepts the detach-shaped call (``start_new_session=
    True``, used by ``_spawn_detached_hook_resync``) and records it without
    actually spawning a child; all other ``Popen`` calls (e.g. from
    ``subprocess.run`` for git) are forwarded to the real ``Popen``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._real_popen = subprocess.Popen

    def __call__(self, *args, **kwargs):
        if kwargs.get("start_new_session"):
            self.calls.append((args, kwargs))
            return MagicMock()
        return self._real_popen(*args, **kwargs)

    def assert_called_once(self) -> None:
        assert len(self.calls) == 1, f"expected exactly one detach spawn, got {len(self.calls)}"

    def assert_not_called(self) -> None:
        assert len(self.calls) == 0, f"expected no detach spawn, got {len(self.calls)}"

    @property
    def call_args(self) -> tuple[tuple, dict]:
        return self.calls[-1]


def _git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )
    (repo / "init.sql").write_text("CREATE TABLE INIT_TABLE (id INT);\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _commit_all(repo: Path, msg: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Hook path + no server: detach
# ---------------------------------------------------------------------------


class TestHookPathDetachesWhenNoServer:
    """Hook path (--notify) with no live server detaches into the background."""

    def test_returns_promptly_and_prints_single_background_notice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Detach path prints exactly one 'resyncing in background' line and exits 0."""
        repo = _git_repo(tmp_path)
        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        (repo / "added.sql").write_text("CREATE TABLE ADDED_TABLE (id INT);\n")
        sha_b = _commit_all(repo, "add table")

        db_path = tmp_path / "graph.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        mock_popen = _DetachPopenSpy()
        monkeypatch.setattr(reindex_module.subprocess, "Popen", mock_popen)

        result = runner.invoke(
            app,
            [
                "reindex",
                str(repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--quiet",
                "--notify",
            ],
        )

        assert result.exit_code == 0, result.output
        background_lines = [
            line for line in result.output.splitlines() if "resyncing graph in background" in line
        ]
        assert len(background_lines) == 1, (
            f"expected exactly one background-resync notice, got: {result.output!r}"
        )
        assert "sqlcg: resyncing graph in background (log:" in background_lines[0]

        # Direct synchronous path must NOT have run: no Popen call means no
        # backend opened in-process for the heavy resync.
        mock_popen.assert_called_once()

    def test_spawns_detached_child_with_session_guard_env_and_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The spawned child gets start_new_session=True, the guard env var, and a log file."""
        repo = _git_repo(tmp_path)
        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        (repo / "added2.sql").write_text("CREATE TABLE ADDED_TABLE2 (id INT);\n")
        sha_b = _commit_all(repo, "add table 2")

        db_path = tmp_path / "graph2.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        mock_popen = _DetachPopenSpy()
        monkeypatch.setattr(reindex_module.subprocess, "Popen", mock_popen)

        result = runner.invoke(
            app,
            [
                "reindex",
                str(repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--quiet",
                "--notify",
            ],
        )

        assert result.exit_code == 0, result.output
        mock_popen.assert_called_once()
        _args, kwargs = mock_popen.call_args

        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"][reindex_module._HOOK_DETACHED_ENV] == "1"

        # Log file path is derived from get_db_path()'s parent (db_path.parent).
        expected_log = db_path.parent / "hook-resync.log"
        assert expected_log.parent.exists()
        # stdout/stderr are open file handles pointed at the log file.
        assert kwargs["stdout"].name == str(expected_log)
        assert kwargs["stderr"].name == str(expected_log)


# ---------------------------------------------------------------------------
# Respawn guard: SQLCG_HOOK_DETACHED=1 -> direct synchronous path, no re-spawn
# ---------------------------------------------------------------------------


class TestRespawnGuardHonoured:
    """With SQLCG_HOOK_DETACHED=1 set, the detached child runs synchronously."""

    def test_guard_env_set_runs_direct_path_without_respawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """SQLCG_HOOK_DETACHED=1 -> no Popen call, graph is updated synchronously."""
        from sqlcg.core.duckdb_backend import DuckDBBackend
        from sqlcg.indexer.indexer import Indexer

        repo = _git_repo(tmp_path)
        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        db_path = tmp_path / "graph3.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        backend = DuckDBBackend(str(db_path))
        backend.init_schema()
        indexer = Indexer()
        indexer.index_repo(repo, "ansi", backend)
        backend.set_indexed_sha(sha_a)
        backend.close()

        (repo / "added3.sql").write_text("CREATE TABLE ADDED_TABLE3 (id INT);\n")
        sha_b = _commit_all(repo, "add table 3")

        mock_popen = _DetachPopenSpy()
        monkeypatch.setattr(reindex_module.subprocess, "Popen", mock_popen)
        monkeypatch.setenv(reindex_module._HOOK_DETACHED_ENV, "1")

        result = runner.invoke(
            app,
            [
                "reindex",
                str(repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--quiet",
                "--notify",
            ],
        )

        assert result.exit_code == 0, result.output
        mock_popen.assert_not_called()

        # Observable: the new table is now in the graph.
        check = DuckDBBackend(str(db_path))
        check.init_schema()
        rows = check.run_read(
            'SELECT name FROM "SqlTable" WHERE name = ?', {"name": "added_table3"}
        )
        check.close()
        assert rows, "added_table3 must be present in the graph after the synchronous direct path"


# ---------------------------------------------------------------------------
# Manual path (no --notify) never detaches
# ---------------------------------------------------------------------------


class TestManualPathNeverDetaches:
    """Manual `sqlcg reindex` (no --notify) stays fully synchronous."""

    def test_manual_reindex_does_not_spawn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """No --notify -> no Popen call, direct synchronous path runs."""
        from sqlcg.core.duckdb_backend import DuckDBBackend
        from sqlcg.indexer.indexer import Indexer

        repo = _git_repo(tmp_path)
        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        db_path = tmp_path / "graph4.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        backend = DuckDBBackend(str(db_path))
        backend.init_schema()
        indexer = Indexer()
        indexer.index_repo(repo, "ansi", backend)
        backend.set_indexed_sha(sha_a)
        backend.close()

        (repo / "added4.sql").write_text("CREATE TABLE ADDED_TABLE4 (id INT);\n")
        sha_b = _commit_all(repo, "add table 4")

        mock_popen = _DetachPopenSpy()
        monkeypatch.setattr(reindex_module.subprocess, "Popen", mock_popen)

        result = runner.invoke(
            app,
            [
                "reindex",
                str(repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--quiet",
            ],
        )

        assert result.exit_code == 0, result.output
        mock_popen.assert_not_called()

        check = DuckDBBackend(str(db_path))
        check.init_schema()
        rows = check.run_read(
            'SELECT name FROM "SqlTable" WHERE name = ?', {"name": "added_table4"}
        )
        check.close()
        assert rows, "added_table4 must be present after the manual synchronous path"


# ---------------------------------------------------------------------------
# Routed path (server live) never spawns
# ---------------------------------------------------------------------------


class TestRoutedPathNeverSpawns:
    """When the socket probe succeeds, no detach Popen happens (existing behaviour)."""

    def test_routed_reindex_does_not_spawn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """_try_route_reindex_via_server returning True short-circuits before detach."""
        repo = _git_repo(tmp_path)
        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        (repo / "added5.sql").write_text("CREATE TABLE ADDED_TABLE5 (id INT);\n")
        sha_b = _commit_all(repo, "add table 5")

        db_path = tmp_path / "graph5.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        mock_popen = _DetachPopenSpy()
        monkeypatch.setattr(reindex_module.subprocess, "Popen", mock_popen)
        monkeypatch.setattr(reindex_module, "_try_route_reindex_via_server", lambda **kwargs: True)

        result = runner.invoke(
            app,
            [
                "reindex",
                str(repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--quiet",
                "--notify",
            ],
        )

        assert result.exit_code == 0, result.output
        mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Lock-retry policy in the detached child
# ---------------------------------------------------------------------------


class TestLockRetryPolicy:
    """Bounded retry/backoff when the detached child hits "Database is locked"."""

    def test_bounded_retries_then_clear_failure_message(self, monkeypatch: pytest.MonkeyPatch):
        """get_backend() always raises IOException -> bounded retries, clear failure log."""
        attempts: list[int] = []

        def _always_locked() -> None:
            attempts.append(1)
            raise duckdb.IOException("Database is locked — another sqlcg process is running.")

        monkeypatch.setattr(reindex_module, "time", reindex_module.time)
        monkeypatch.setattr(reindex_module.time, "sleep", lambda _s: None)

        # Patch get_backend at the source module since _open_backend_with_lock_retry
        # imports it locally inside the function.
        import sqlcg.core.config as config_module

        monkeypatch.setattr(config_module, "get_backend", _always_locked)

        with pytest.raises(Exception) as exc_info:
            reindex_module._open_backend_with_lock_retry()

        # typer.Exit(1) is raised on exhaustion.
        import typer

        assert isinstance(exc_info.value, typer.Exit)
        assert exc_info.value.exit_code == 1
        assert len(attempts) == reindex_module._LOCK_RETRY_ATTEMPTS

    def test_succeeds_after_transient_lock(self, monkeypatch: pytest.MonkeyPatch):
        """get_backend() raises IOException once, then succeeds -> retry recovers."""
        calls = {"n": 0}
        sentinel_backend = MagicMock()

        def _locked_then_ok():
            calls["n"] += 1
            if calls["n"] == 1:
                raise duckdb.IOException("Database is locked — another sqlcg process is running.")
            return sentinel_backend

        monkeypatch.setattr(reindex_module.time, "sleep", lambda _s: None)

        import sqlcg.core.config as config_module

        monkeypatch.setattr(config_module, "get_backend", _locked_then_ok)

        result = reindex_module._open_backend_with_lock_retry()

        assert result is sentinel_backend
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Respawn argv resolution (python -m sqlcg case)
# ---------------------------------------------------------------------------


class TestRespawnArgvResolution:
    """_respawn_argv handles both the resolved-binary and `python -m` cases."""

    def test_non_executable_argv0_falls_back_to_python_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A non-executable sys.argv[0] (e.g. __main__.py) re-execs via -m sqlcg."""
        fake_main = tmp_path / "__main__.py"
        fake_main.write_text("# not executable\n")
        monkeypatch.setattr(reindex_module.sys, "argv", [str(fake_main), "reindex", "--notify"])

        argv = reindex_module._respawn_argv()

        assert argv[0] == reindex_module.sys.executable
        assert argv[1:3] == ["-m", "sqlcg"]
        assert argv[3:] == ["reindex", "--notify"]

    def test_executable_argv0_reused_as_is(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """An executable resolved-binary sys.argv[0] is re-used directly."""
        fake_bin = tmp_path / "sqlcg"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        monkeypatch.setattr(reindex_module.sys, "argv", [str(fake_bin), "reindex", "--notify"])

        argv = reindex_module._respawn_argv()

        assert argv == [str(fake_bin), "reindex", "--notify"]


# ---------------------------------------------------------------------------
# Hook resync log path derivation
# ---------------------------------------------------------------------------


class TestHookResyncLogPath:
    """_hook_resync_log_path is derived from get_db_path(), never hardcoded."""

    def test_log_path_under_db_path_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The log file lives in get_db_path().parent / 'hook-resync.log'."""
        db_path = tmp_path / "subdir" / "graph.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        log_path = reindex_module._hook_resync_log_path()

        assert log_path == db_path.parent / "hook-resync.log"


# ---------------------------------------------------------------------------
# #1 — loud foreground preflight on schema/version skew before detaching
# ---------------------------------------------------------------------------


def _seed_db_with_schema_version(db_path: Path, version: str) -> None:
    """Create a graph whose stored SchemaVersion row has the given version.

    Writes the row directly so we can simulate a *stale-binary* graph whose
    stored schema version differs from the current build's ``SCHEMA_VERSION``.
    """
    from sqlcg.core.duckdb_backend import DuckDBBackend

    backend = DuckDBBackend(str(db_path))
    backend.init_schema()
    backend._conn.execute('DELETE FROM "SchemaVersion"')
    backend._conn.execute(
        'INSERT INTO "SchemaVersion" (version, indexed_sha, sqlcg_version) VALUES (?, ?, ?)',
        [version, "deadbeef", "1.25.6"],
    )
    backend.close()


class TestHookPreflightSchemaSkew:
    """A stale-binary schema skew on the hook path fails LOUDLY, never silently (#1)."""

    def test_schema_mismatch_exits_nonzero_with_message_and_no_detach(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Stored schema != current -> non-zero exit, visible message, NO detach spawn.

        Inverts the #1 silent-exit-0 failure mode: before the fix the schema gate
        ran in the detached child (log file only); now the foreground preflight
        surfaces it. Guards the git-hook binary-resolution plan
        ([plan doc](plan/sprints/bugfix_githook_binary_resolution.md)).
        """
        repo = _git_repo(tmp_path)
        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        (repo / "added.sql").write_text("CREATE TABLE ADDED (id INT);\n")
        sha_b = _commit_all(repo, "add table")

        db_path = tmp_path / "stale.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))
        _seed_db_with_schema_version(db_path, "8")  # stale: build needs "10"

        mock_popen = _DetachPopenSpy()
        monkeypatch.setattr(reindex_module.subprocess, "Popen", mock_popen)

        result = runner.invoke(
            app,
            [
                "reindex",
                str(repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--quiet",
                "--notify",
            ],
        )

        assert result.exit_code != 0, result.output
        assert "schema is v8" in result.output
        assert "hook binary is stale" in result.output
        assert "sqlcg git install-hooks" in result.output
        # The whole point: it did NOT detach a doomed background resync.
        mock_popen.assert_not_called()

    def test_matching_schema_detaches_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Stored schema == current -> preflight is silent, detach proceeds, exit 0.

        Regression guard that the preflight does not break the legitimate async
        path. Guards the git-hook binary-resolution plan
        ([plan doc](plan/sprints/bugfix_githook_binary_resolution.md)).
        """
        from sqlcg.core.schema import SCHEMA_VERSION

        repo = _git_repo(tmp_path)
        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        (repo / "added.sql").write_text("CREATE TABLE ADDED (id INT);\n")
        sha_b = _commit_all(repo, "add table")

        db_path = tmp_path / "current.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))
        _seed_db_with_schema_version(db_path, SCHEMA_VERSION)

        mock_popen = _DetachPopenSpy()
        monkeypatch.setattr(reindex_module.subprocess, "Popen", mock_popen)

        result = runner.invoke(
            app,
            [
                "reindex",
                str(repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--quiet",
                "--notify",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "hook binary is stale" not in result.output
        mock_popen.assert_called_once()

    def test_locked_db_falls_through_to_detach_without_blocking_or_false_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """B1 hot-path safety: lock held -> single fast open, fall through to detach.

        Simulates a concurrent resync holding the DuckDB exclusive lock: the
        preflight's open raises IOException. It MUST NOT retry ~31 s (a #77-class
        stall) and MUST NOT emit a false stale-schema error — it falls through to
        the detach path unchanged. Proves we did not reintroduce #77. Guards the
        git-hook binary-resolution plan
        ([plan doc](plan/sprints/bugfix_githook_binary_resolution.md)).
        """
        repo = _git_repo(tmp_path)
        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        (repo / "added.sql").write_text("CREATE TABLE ADDED (id INT);\n")
        sha_b = _commit_all(repo, "add table")

        db_path = tmp_path / "locked.db"
        monkeypatch.setenv("SQLCG_DB_PATH", str(db_path))

        # Make the preflight's read-only open raise IOException (lock held), and
        # count calls so we can assert it was a SINGLE attempt (no 31 s retry).
        open_calls = {"n": 0}

        def _always_locked(read_only: bool = False):
            open_calls["n"] += 1
            raise duckdb.IOException("Conflicting lock is held — another sqlcg process is running.")

        import sqlcg.core.config as config_module

        monkeypatch.setattr(config_module, "get_backend", _always_locked)
        # If the preflight ever slept (retry), fail loudly — it must be a single
        # bounded open, not the lock-retry path.
        monkeypatch.setattr(
            reindex_module.time,
            "sleep",
            lambda _s: (_ for _ in ()).throw(
                AssertionError("preflight must not sleep/retry on a held lock")
            ),
        )

        mock_popen = _DetachPopenSpy()
        monkeypatch.setattr(reindex_module.subprocess, "Popen", mock_popen)

        result = runner.invoke(
            app,
            [
                "reindex",
                str(repo),
                "--from",
                sha_a,
                "--to",
                sha_b,
                "--dialect",
                "ansi",
                "--quiet",
                "--notify",
            ],
        )

        assert result.exit_code == 0, result.output
        # Fell through to detach, no false stale-schema error.
        assert "hook binary is stale" not in result.output
        mock_popen.assert_called_once()
        # Single bounded open — the preflight opened exactly once (not 5x retry).
        assert open_calls["n"] == 1, f"preflight opened {open_calls['n']} times (must be 1)"
