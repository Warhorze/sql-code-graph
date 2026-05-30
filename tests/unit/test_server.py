"""Unit tests for MCP server configuration."""

import os
import sys

from sqlcg.server.server import _configure_mcp_logging


def test_configure_mcp_logging_redirects_stdout():
    """Test that _configure_mcp_logging() redirects stdout to stderr.

    This is critical for MCP protocol integrity, as the protocol uses
    stdout for JSON-RPC messages. The function is called inside main(),
    not at module scope (to preserve fd 1 capture order).
    """
    # Save original stdout
    original_stdout = sys.stdout

    try:
        # Reset stdout to original before calling
        sys.stdout = original_stdout

        # Call the function
        _configure_mcp_logging()

        # Assert that stdout is now stderr
        assert sys.stdout is sys.stderr
    finally:
        # Restore original stdout
        sys.stdout = original_stdout


def test_configure_mcp_logging_does_not_close_fd1():
    """Scenario B: _configure_mcp_logging does not destroy fd 1.

    After redirecting sys.stdout to sys.stderr, fd 1 (the underlying OS file
    descriptor) must still be open and writable. _real_stdout_buffer uses a
    dup of fd 1, so the original fd 1 must remain open.
    """
    original_stdout = sys.stdout
    try:
        _configure_mcp_logging()
        # os.write(1, b"") must not raise — fd 1 is still open
        os.write(1, b"")
    finally:
        sys.stdout = original_stdout
