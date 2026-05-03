"""Exceptions raised by MCP server tools."""


class NotIndexedError(RuntimeError):
    """Raised when graph has no indexed repos.

    This error indicates that no repositories have been indexed yet.
    Users should run `sqlcg index <path>` first to populate the graph.
    """

    pass


class InvalidColumnRefError(ValueError):
    """Raised for invalid column reference format.

    Expected format: "table.column" or "catalog.db.table.column".
    """

    pass
