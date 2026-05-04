"""Configuration management for sqlcg."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from sqlcg.core.graph_db import GraphBackend


class KuzuConfig(BaseModel):
    """Configuration for KùzuDB backend."""

    db_path: Path = Field(default_factory=lambda: Path.home() / ".sqlcg" / "graph.db")

    @classmethod
    def from_env(cls) -> "KuzuConfig":
        """Load KùzuDB config from environment variables.

        Returns:
            KuzuConfig instance with environment-overridden values if present.
        """
        env_path = os.getenv("SQLCG_DB_PATH")
        return cls(db_path=Path(env_path)) if env_path else cls()


class Neo4jConfig(BaseModel):
    """Configuration for Neo4j backend."""

    uri: str = Field(default="bolt://localhost:7687")
    user: str = Field(default="neo4j")
    password: str = Field(default="password")

    @classmethod
    def from_env(cls) -> "Neo4jConfig":
        """Load Neo4j config from environment variables.

        Returns:
            Neo4jConfig instance with environment-overridden values if present.
        """
        return cls(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_USER", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", "password"),
        )


def get_db_path() -> Path:
    """Get the database path from environment or use default.

    Returns:
        Path to the KùzuDB database file
    """
    return KuzuConfig.from_env().db_path


def get_backend() -> "GraphBackend":
    """Get a graph backend instance respecting the SQLCG_BACKEND env var.

    Returns:
        A GraphBackend instance (KuzuBackend by default, or Neo4jBackend)

    Raises:
        ValueError: If backend type is not recognized
    """
    backend_type = os.getenv("SQLCG_BACKEND", "kuzu")

    if backend_type == "kuzu":
        from sqlcg.core.kuzu_backend import KuzuBackend

        kuzu_cfg = KuzuConfig.from_env()
        return KuzuBackend(str(kuzu_cfg.db_path))
    elif backend_type == "neo4j":
        from sqlcg.core.neo4j_backend import Neo4jBackend

        neo4j_cfg = Neo4jConfig.from_env()
        return Neo4jBackend(neo4j_cfg.uri, neo4j_cfg.user, neo4j_cfg.password)
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")
