"""SQL hashing utilities."""

import hashlib


def hash_sql(sql: str) -> str:
    """Generate a SHA-256 hash of SQL content.

    The SQL is normalized by stripping leading and trailing whitespace before hashing.

    Args:
        sql: SQL statement string

    Returns:
        SHA-256 hex digest of the normalized SQL bytes
    """
    normalized_sql = sql.strip()
    return hashlib.sha256(normalized_sql.encode("utf-8")).hexdigest()
