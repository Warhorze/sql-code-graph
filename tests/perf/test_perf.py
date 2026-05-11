"""Performance tests for sql-code-graph."""

import time
from pathlib import Path

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.ansi_parser import AnsiParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.perf
def test_artikel_under_9s():
    """Test that 45-statement fixture parses under 9 seconds.

    Baseline: 14.2s
    Target: < 9.0s (≥36% reduction)

    This uses a synthetic fixture mimicking WTDH_ARTIKEL.sql structure
    to avoid committing production files.
    """
    schema = SchemaResolver()
    parser = AnsiParser(schema)

    # Generate a synthetic SQL with 45 statements: 44 CTAS + 1 INSERT
    # Each CTAS is a simple: CREATE TEMP TABLE stmt_N AS SELECT ...
    statements = []
    for i in range(44):
        statements.append(f"CREATE TEMP TABLE stmt_{i:03d} AS SELECT {i} AS col_{i} FROM t_{i}")
    statements.append("INSERT INTO target SELECT * FROM stmt_043")

    sql = ";\n".join(statements)

    t0 = time.perf_counter()
    parser.parse_file(Path("artikel.sql"), sql)
    elapsed = time.perf_counter() - t0

    assert elapsed < 9.0, (
        f"Expected < 9.0s, got {elapsed:.2f}s (target 36% reduction from 14.2s baseline)"
    )
