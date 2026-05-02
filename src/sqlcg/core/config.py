"""Configuration management for sqlcg."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from sqlcg.core.graph_db import GraphBackend


def _load_env() -> None:
    """Load environment variables from .env file."""
    load_dotenv()


_load_env()


def get_db_path() -> Path:
    """Get the database path from environment or use default.

    Returns:
        Path to the KùzuDB database file
    """
    default_path = Path.home() / ".sqlcg" / "graph.db"
    env_path = os.getenv("SQLCG_DB_PATH")
    if env_path:
        return Path(env_path)
    return default_path


def get_backend_type() -> str:
    """Get the backend type from environment or use default.

    Returns:
        Backend name ("kuzu" or "neo4j")
    """
    return os.getenv("SQLCG_BACKEND", "kuzu")


def get_backend() -> "GraphBackend":
    """Get a graph backend instance respecting the SQLCG_BACKEND env var.

    Returns:
        A GraphBackend instance (KuzuBackend by default, or Neo4jBackend)

    Raises:
        ValueError: If backend type is not recognized
    """
    backend_type = get_backend_type()
    db_path = get_db_path()

    if backend_type == "kuzu":
        from sqlcg.core.kuzu_backend import KuzuBackend

        return KuzuBackend(str(db_path))
    elif backend_type == "neo4j":
        from sqlcg.core.neo4j_backend import Neo4jBackend

        # Read Neo4j connection params from env vars
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "password")
        return Neo4jBackend(uri, user, password)
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")
