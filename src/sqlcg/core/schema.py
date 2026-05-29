"""KùzuDB schema definition for sqlcg graph."""

from enum import StrEnum
from importlib.resources import files

SCHEMA_VERSION = "3"


class NodeLabel(StrEnum):
    REPO = "Repo"
    FILE = "File"
    TABLE = "SqlTable"
    COLUMN = "SqlColumn"
    QUERY = "SqlQuery"
    SCHEMA_VERSION = "SchemaVersion"


class RelType(StrEnum):
    BELONGS_TO = "BELONGS_TO"
    DEFINED_IN = "DEFINED_IN"
    QUERY_DEFINED_IN = "QUERY_DEFINED_IN"
    HAS_COLUMN = "HAS_COLUMN"
    SELECTS_FROM = "SELECTS_FROM"
    INSERTS_INTO = "INSERTS_INTO"
    DELETES_FROM = "DELETES_FROM"
    UPDATES = "UPDATES"
    COLUMN_LINEAGE = "COLUMN_LINEAGE"
    DECLARES = "DECLARES"
    STAR_SOURCE = "STAR_SOURCE"


# Backward-compatible aliases
NODE_REPO = NodeLabel.REPO
NODE_FILE = NodeLabel.FILE
NODE_TABLE = NodeLabel.TABLE
NODE_COLUMN = NodeLabel.COLUMN
NODE_QUERY = NodeLabel.QUERY
NODE_SCHEMA_VERSION = NodeLabel.SCHEMA_VERSION

REL_DEFINED_IN = RelType.DEFINED_IN
REL_HAS_COLUMN = RelType.HAS_COLUMN
REL_SELECTS_FROM = RelType.SELECTS_FROM
REL_INSERTS_INTO = RelType.INSERTS_INTO
REL_DELETES_FROM = RelType.DELETES_FROM
REL_UPDATES = RelType.UPDATES
REL_COLUMN_LINEAGE = RelType.COLUMN_LINEAGE
REL_DECLARES = RelType.DECLARES

SCHEMA_DDL: str = files("sqlcg.core").joinpath("schema.cypher").read_text(encoding="utf-8")
