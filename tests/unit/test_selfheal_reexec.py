"""Unit tests for PR 2 self-heal re-exec machinery.

Covers T4–T6, T-F8, T-F2-argv0 from
plan/sprints/mcp_server_self_healing.md §Test Strategy (PR 2).

- T4: maybe_self_heal throttle — disk scanned at most once per interval;
      second call within the window skips the scan.
- T5: on skew, maybe_self_heal sets the injected event and logs at WARNING.
- T6: unreadable on-disk version → event NOT set (F1 guard).
- T-F8: _reexec with SQLCG_SELF_HEAL_GENERATION>=3 → logs ERROR, does NOT call execv.
- T-F2-argv0: _reexec with non-executable _resolved_argv0 → logs ERROR, does NOT call execv.
- misc: register_self_heal_event wires the module-level event.

Guards plan/sprints/mcp_server_self_healing.md §Phase 2 / Steps 2.1–2.2.
"""

from __future__ import annotations

import os

import anyio

import sqlcg.server.selfheal as selfheal
from sqlcg import __version__

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event():
    """Return a real anyio.Event usable outside an event loop for .is_set() checks."""
    return anyio.Event()


# ---------------------------------------------------------------------------
# T4 — throttle: disk scanned at most once per interval
# ---------------------------------------------------------------------------


class TestMaybeSelfHealThrottle:
    """T4: maybe_self_heal throttle touches disk once per _SKEW_CHECK_INTERVAL_S window.

    Guards plan/sprints/mcp_server_self_healing.md §D2 / T4.
    """

    def test_second_call_within_window_skips_disk_scan(self, monkeypatch):
        """Two calls within the throttle window produce exactly one disk read.

        Guards plan/sprints/mcp_server_self_healing.md T4.
        """
        call_count = {"n": 0}
        original_version = __version__

        def counting_read_ondisk():
            call_count["n"] += 1
            return original_version  # no skew

        # Reset last check so the first call goes through.
        monkeypatch.setattr(selfheal, "_last_skew_check", 0.0)
        monkeypatch.setattr(selfheal, "read_ondisk_version", counting_read_ondisk)

        selfheal.maybe_self_heal()
        selfheal.maybe_self_heal()  # within throttle window

        assert call_count["n"] == 1, f"Expected 1 disk read within window, got {call_count['n']}"

    def test_call_after_throttle_expires_scans_again(self, monkeypatch):
        """A call after the interval expires triggers a fresh disk scan.

        Guards plan/sprints/mcp_server_self_healing.md T4.
        """
        call_count = {"n": 0}

        def counting_read_ondisk():
            call_count["n"] += 1
            return __version__  # no skew

        # Set last check far in the past so both windows are "expired".
        monkeypatch.setattr(selfheal, "_last_skew_check", 0.0)
        monkeypatch.setattr(selfheal, "read_ondisk_version", counting_read_ondisk)
        monkeypatch.setattr(selfheal, "_SKEW_CHECK_INTERVAL_S", 0.0)  # instant expiry

        selfheal.maybe_self_heal()  # first scan
        # Force expiry by resetting last check to 0.
        monkeypatch.setattr(selfheal, "_last_skew_check", 0.0)
        selfheal.maybe_self_heal()  # second scan (window expired)

        assert call_count["n"] == 2, (
            f"Expected 2 disk reads (one per expired window), got {call_count['n']}"
        )


# ---------------------------------------------------------------------------
# T5 — skew detected: event set + WARNING logged
# ---------------------------------------------------------------------------


class TestMaybeSelfHealSkewSetsEvent:
    """T5: on skew, maybe_self_heal sets the injected event and logs at WARNING.

    Guards plan/sprints/mcp_server_self_healing.md §D3 / T5.
    """

    def test_skew_sets_event(self, monkeypatch, caplog):
        """Skew → _self_heal_event.set() is called.

        Guards plan/sprints/mcp_server_self_healing.md T5.
        """
        import logging

        event = _make_event()
        monkeypatch.setattr(selfheal, "_last_skew_check", 0.0)
        monkeypatch.setattr(selfheal, "_self_heal_event", event)
        monkeypatch.setattr(selfheal, "read_ondisk_version", lambda: "99.0.0-new")

        with caplog.at_level(logging.WARNING, logger="sqlcg.server.selfheal"):
            selfheal.maybe_self_heal()

        assert event.is_set(), "Expected event.is_set() after skew detection"

    def test_skew_logs_both_versions(self, monkeypatch, caplog):
        """Skew logs running and ondisk versions at WARNING.

        Guards plan/sprints/mcp_server_self_healing.md T5.
        """
        import logging

        event = _make_event()
        monkeypatch.setattr(selfheal, "_last_skew_check", 0.0)
        monkeypatch.setattr(selfheal, "_self_heal_event", event)
        monkeypatch.setattr(selfheal, "read_ondisk_version", lambda: "99.0.0-new")

        with caplog.at_level(logging.WARNING, logger="sqlcg.server.selfheal"):
            selfheal.maybe_self_heal()

        log_text = " ".join(caplog.messages)
        assert __version__ in log_text, f"Expected running version {__version__!r} in log"
        assert "99.0.0-new" in log_text, "Expected ondisk version '99.0.0-new' in log"

    def test_no_skew_does_not_set_event(self, monkeypatch):
        """No skew → event remains unset.

        Guards plan/sprints/mcp_server_self_healing.md T5 (negative case).
        """
        event = _make_event()
        monkeypatch.setattr(selfheal, "_last_skew_check", 0.0)
        monkeypatch.setattr(selfheal, "_self_heal_event", event)
        monkeypatch.setattr(selfheal, "read_ondisk_version", lambda: __version__)

        selfheal.maybe_self_heal()

        assert not event.is_set(), "Event must not be set when no skew is detected"


# ---------------------------------------------------------------------------
# T6 — unreadable on-disk version: event NOT set (F1 guard)
# ---------------------------------------------------------------------------


class TestMaybeSelfHealUnreadable:
    """T6: unreadable on-disk version → event NOT set (F1 guard).

    Guards plan/sprints/mcp_server_self_healing.md §F1 / T6.
    """

    def test_unreadable_ondisk_does_not_set_event(self, monkeypatch):
        """read_ondisk_version returning None → event stays unset.

        Guards plan/sprints/mcp_server_self_healing.md T6 / F1.
        """
        event = _make_event()
        monkeypatch.setattr(selfheal, "_last_skew_check", 0.0)
        monkeypatch.setattr(selfheal, "_self_heal_event", event)
        monkeypatch.setattr(selfheal, "read_ondisk_version", lambda: None)

        selfheal.maybe_self_heal()

        assert not event.is_set(), "Event must not be set when ondisk version is None (F1)"


# ---------------------------------------------------------------------------
# T-F8 — generation cap: refuses execv at generation >= 3
# ---------------------------------------------------------------------------


class TestReexecGenerationGuard:
    """T-F8: _reexec refuses to exec at SQLCG_SELF_HEAL_GENERATION >= 3 (F8, A4).

    Guards plan/sprints/mcp_server_self_healing.md §F8 / T-F8.
    """

    def test_generation_3_does_not_call_execv(self, monkeypatch, caplog, tmp_path):
        """SQLCG_SELF_HEAL_GENERATION=3 → logs ERROR, does NOT call os.execv.

        Guards plan/sprints/mcp_server_self_healing.md T-F8 / A4.
        """
        import logging

        import sqlcg.server.server as srv

        execv_calls = []

        # Provide a valid executable so the A5 guard doesn't also fire.
        fake_exe = tmp_path / "fake_sqlcg"
        fake_exe.write_text("#!/bin/sh\necho ok\n")
        fake_exe.chmod(0o755)

        monkeypatch.setattr(srv, "_resolved_argv0", str(fake_exe))
        monkeypatch.setenv("SQLCG_SELF_HEAL_GENERATION", "3")
        monkeypatch.setattr(os, "execv", lambda *a, **kw: execv_calls.append(a))

        with caplog.at_level(logging.ERROR, logger="sqlcg.server.server"):
            srv._reexec()

        assert len(execv_calls) == 0, "os.execv must NOT be called at generation >= 3"
        log_text = " ".join(caplog.messages)
        assert "SQLCG_SELF_HEAL_GENERATION" in log_text or "generation" in log_text.lower(), (
            "Expected generation guard message in ERROR log"
        )

    def test_generation_2_would_exec(self, monkeypatch, tmp_path):
        """SQLCG_SELF_HEAL_GENERATION=2 → proceeds to exec (generation < 3).

        Guards plan/sprints/mcp_server_self_healing.md A4 (boundary).
        """
        import sqlcg.server.server as srv

        execv_calls = []
        fake_exe = tmp_path / "fake_sqlcg"
        fake_exe.write_text("#!/bin/sh\necho ok\n")
        fake_exe.chmod(0o755)

        monkeypatch.setattr(srv, "_resolved_argv0", str(fake_exe))
        monkeypatch.setenv("SQLCG_SELF_HEAL_GENERATION", "2")
        monkeypatch.setattr(os, "execv", lambda *a, **kw: execv_calls.append(a))

        srv._reexec()

        assert len(execv_calls) == 1, "os.execv must be called at generation 2 (< 3)"

    def test_generation_counter_bumped_before_exec(self, monkeypatch, tmp_path):
        """SQLCG_SELF_HEAL_GENERATION is incremented before os.execv is called.

        Guards plan/sprints/mcp_server_self_healing.md A4 (counter increment).
        """
        import sqlcg.server.server as srv

        seen_gen = {}

        fake_exe = tmp_path / "fake_sqlcg"
        fake_exe.write_text("#!/bin/sh\necho ok\n")
        fake_exe.chmod(0o755)

        def recording_execv(path, args):
            seen_gen["val"] = os.environ.get("SQLCG_SELF_HEAL_GENERATION")

        monkeypatch.setattr(srv, "_resolved_argv0", str(fake_exe))
        monkeypatch.setenv("SQLCG_SELF_HEAL_GENERATION", "0")
        monkeypatch.setattr(os, "execv", recording_execv)

        srv._reexec()

        assert seen_gen.get("val") == "1", (
            f"Expected SQLCG_SELF_HEAL_GENERATION='1' at exec time, got {seen_gen.get('val')!r}"
        )


# ---------------------------------------------------------------------------
# T-F2-argv0 — bad argv0: refuses execv (A5 guard)
# ---------------------------------------------------------------------------


class TestReexecArgv0Guard:
    """T-F2-argv0: _reexec with non-executable/missing _resolved_argv0 → logs ERROR, no execv.

    Guards plan/sprints/mcp_server_self_healing.md §A5 / T-F2-argv0.
    """

    def test_none_argv0_does_not_exec(self, monkeypatch, caplog):
        """_resolved_argv0=None → logs ERROR, does NOT call os.execv.

        Guards plan/sprints/mcp_server_self_healing.md A5.
        """
        import logging

        import sqlcg.server.server as srv

        execv_calls = []
        monkeypatch.setattr(srv, "_resolved_argv0", None)
        monkeypatch.setenv("SQLCG_SELF_HEAL_GENERATION", "0")
        monkeypatch.setattr(os, "execv", lambda *a, **kw: execv_calls.append(a))

        with caplog.at_level(logging.ERROR, logger="sqlcg.server.server"):
            srv._reexec()

        assert len(execv_calls) == 0
        assert any("argv0" in m or "executable" in m for m in caplog.messages), (
            "Expected argv0 guard message in ERROR log"
        )

    def test_missing_file_argv0_does_not_exec(self, monkeypatch, tmp_path):
        """_resolved_argv0 pointing at a nonexistent file → no execv.

        Guards plan/sprints/mcp_server_self_healing.md A5.
        """
        import sqlcg.server.server as srv

        execv_calls = []
        monkeypatch.setattr(srv, "_resolved_argv0", str(tmp_path / "no_such_file"))
        monkeypatch.setenv("SQLCG_SELF_HEAL_GENERATION", "0")
        monkeypatch.setattr(os, "execv", lambda *a, **kw: execv_calls.append(a))

        srv._reexec()

        assert len(execv_calls) == 0

    def test_non_executable_file_argv0_does_not_exec(self, monkeypatch, tmp_path):
        """_resolved_argv0 pointing at a file without x-bit → no execv.

        Guards plan/sprints/mcp_server_self_healing.md A5.
        """
        import sqlcg.server.server as srv

        execv_calls = []
        non_exe = tmp_path / "not_executable"
        non_exe.write_text("data")
        non_exe.chmod(0o600)  # readable but not executable

        monkeypatch.setattr(srv, "_resolved_argv0", str(non_exe))
        monkeypatch.setenv("SQLCG_SELF_HEAL_GENERATION", "0")
        monkeypatch.setattr(os, "execv", lambda *a, **kw: execv_calls.append(a))

        srv._reexec()

        assert len(execv_calls) == 0


# ---------------------------------------------------------------------------
# register_self_heal_event wires correctly
# ---------------------------------------------------------------------------


def test_register_self_heal_event_wires_module_level(monkeypatch):
    """register_self_heal_event stores the event so maybe_self_heal can set it.

    Guards plan/sprints/mcp_server_self_healing.md §D3 / Step 2.1.
    """
    event = _make_event()

    # Clear any existing event before the test.
    monkeypatch.setattr(selfheal, "_self_heal_event", None)
    selfheal.register_self_heal_event(event)

    assert selfheal._self_heal_event is event, (
        "register_self_heal_event must wire the exact event object"
    )
