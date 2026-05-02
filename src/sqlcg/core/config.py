"""Configuration management for sqlcg."""

import os
from pathlib import Path

from dotenv import load_dotenv


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


def get_backend() -> str:
    """Get the backend type from environment or use default.

    Returns:
        Backend name ("kuzu" or "neo4j")
    """
    return os.getenv("SQLCG_BACKEND", "kuzu")
