"""Unit tests for MCP server configuration."""

import sys

from sqlcg.server.server import _configure_mcp_logging


def test_configure_mcp_logging_redirects_stdout():
    """Test that _configure_mcp_logging() redirects stdout to stderr.

    This is critical for MCP protocol integrity, as the protocol uses
    stdout for JSON-RPC messages.
    """
    # Save original stdout
    original_stdout = sys.stdout

    try:
        # Reset stdout to original
        sys.stdout = original_stdout

        # Call the function
        _configure_mcp_logging()

        # Assert that stdout is now stderr
        assert sys.stdout is sys.stderr
    finally:
        # Restore original stdout
        sys.stdout = original_stdout
