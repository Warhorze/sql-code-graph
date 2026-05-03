"""MCP server for SQL Code Graph.

Exposes FastMCP tools for lineage queries, pattern search, and indexing.
MCP protocol uses stdout for message transport, so this module redirects
stdout to stderr to prevent user logs from corrupting the protocol stream.
"""

import sys

from mcp.server import FastMCP

from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


def _configure_mcp_logging() -> None:
    """Redirect sys.stdout to sys.stderr to protect MCP protocol.

    MCP uses stdout for JSON-RPC messages. Any user print() or log output
    to stdout would corrupt the protocol. This function must be called before
    mcp.run() and before any code that might print to stdout.
    """
    sys.stdout = sys.stderr


# Protect stdout before importing FastMCP (which may emit output during import)
_configure_mcp_logging()

# Create FastMCP instance at module scope so tools.py can import and register with it
mcp = FastMCP("SQL Code Graph")


def main(db_path: str | None = None) -> None:
    """Start the MCP server.

    Args:
        db_path: Path to KùzuDB database. If None, uses SQLCG_DB_PATH env var
                or ~/.sqlcg/graph.db (via get_db_path in tools module).

    Raises:
        RuntimeError: If tools fail to initialize or FastMCP server fails.
    """
    # Import tools module to trigger tool registration via @mcp.tool() decorators
    import sqlcg.server.tools

    # Initialize the backend singleton used by all tools
    sqlcg.server.tools.init_backend(db_path)

    # Run the MCP server event loop, ensuring backend is closed on shutdown
    try:
        mcp.run()
    finally:
        sqlcg.server.tools.shutdown_backend()
