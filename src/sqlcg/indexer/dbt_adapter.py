"""dbt manifest adapter for schema resolution."""

from pathlib import Path

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.utils.logging import getLogger

logger = getLogger(__name__)


def load_dbt_manifest(manifest_path: Path, schema_resolver: SchemaResolver) -> None:
    """Load dbt manifest and register table schemas.

    Errors are logged, not raised.

    Args:
        manifest_path: Path to dbt manifest.json
        schema_resolver: SchemaResolver instance to populate
    """
    try:
        schema_resolver.add_dbt_manifest(manifest_path)
    except Exception as exc:
        logger.warning("Failed to load dbt manifest %s: %s", manifest_path, exc)
