"""MCP server for SQL Code Graph.

Exposes FastMCP tools for lineage queries, pattern search, and indexing.
MCP protocol uses stdout (fd 1) for JSON-RPC message transport. This module
captures fd 1 as a raw binary buffer BEFORE any logging redirection so that
the captured buffer can be passed explicitly to stdio_server(). This ensures
JSON-RPC frames always go to fd 1 regardless of what sys.stdout points to
at call time.

Ordering invariant (must not change):
  1. os.dup(1) → _real_stdout_buffer        (first — before everything)
  2. from mcp.server import FastMCP          (module-level import)
  3. mcp = FastMCP("SQL Code Graph")         (module-level; tools.py registers here)
  4. main() calls _configure_mcp_logging()   (not at module scope)
"""

import os
import sys

# Capture the real fd 1 binary stream FIRST — before _configure_mcp_logging()
# (which replaces sys.stdout) AND before FastMCP("SQL Code Graph") construction.
# stdio_server() receives this explicitly so JSON-RPC frames go to fd 1
# regardless of what sys.stdout points to afterward.
# Guards against the v1.0.0/v1.0.1 regression where frames went to fd 2.
_real_stdout_buffer = os.fdopen(os.dup(1), "wb", buffering=0)

from dotenv import load_dotenv  # noqa: E402
from mcp.server import FastMCP  # noqa: E402

from sqlcg.utils.logging import getLogger  # noqa: E402

logger = getLogger(__name__)

# Create FastMCP instance at module scope so tools.py can import and register with it.
# This is safe because _real_stdout_buffer has already captured fd 1 above.
mcp = FastMCP("SQL Code Graph")


def _configure_mcp_logging() -> None:
    """Redirect sys.stdout to sys.stderr and configure logging to stderr.

    sys.stdout is replaced with sys.stderr so that any stray print() call
    does not pollute fd 1 (reserved for MCP JSON-RPC frames).
    The real fd 1 binary stream is captured in _real_stdout_buffer at module
    top before this replacement and passed explicitly to stdio_server().

    Must be called inside main(), not at module scope, so that
    _real_stdout_buffer captures fd 1 before the redirect.
    """
    import logging

    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)


async def _run_stdio_async_with_real_stdout() -> None:
    """Run the MCP server loop with JSON-RPC frames explicitly on fd 1.

    Bypasses FastMCP.run_stdio_async() (which uses sys.stdout at call time)
    and drives the server loop directly with the captured _real_stdout_buffer.
    """
    from io import TextIOWrapper

    import anyio
    from mcp.server.stdio import stdio_server

    stdout_text = TextIOWrapper(_real_stdout_buffer, encoding="utf-8", line_buffering=False)
    async with stdio_server(stdout=anyio.wrap_file(stdout_text)) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


def main(db_path: str | None = None) -> None:
    """Start the MCP server.

    Args:
        db_path: Path to KùzuDB database. If None, uses SQLCG_DB_PATH env var
                or ~/.sqlcg/graph.db (via get_db_path in tools module).
    """
    import anyio

    # Must be first — redirects sys.stdout → sys.stderr so stray prints don't
    # corrupt fd 1. _real_stdout_buffer was already captured at module top.
    _configure_mcp_logging()

    load_dotenv()

    # Import tools module to trigger tool registration via @mcp.tool() decorators
    import sqlcg.server.tools

    # Initialize the backend singleton used by all tools
    sqlcg.server.tools.init_backend(db_path)

    try:
        anyio.run(_run_stdio_async_with_real_stdout)
    finally:
        sqlcg.server.tools.shutdown_backend()
