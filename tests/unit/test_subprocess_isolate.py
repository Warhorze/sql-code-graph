"""T-09-04: Subprocess isolation tests.

Tests that multiprocessing.Process with spawn context properly:
1. Hard-kills a slow parser when timeout fires
2. Round-trips ParsedFile through the queue
3. Propagates exceptions from the worker
4. Bypasses subprocess when timeout=0
5. Handles worker exit without result gracefully
"""

import os
import pickle
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from sqlcg.indexer.indexer import Indexer
from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.base import ParsedFile
from sqlcg.parsers.snowflake_parser import SnowflakeParser


# Module-level classes for pickle compatibility
class SlowParser:
    """Test parser that sleeps for a long time."""

    DIALECT = "snowflake"

    def __init__(self, schema_resolver=None):
        self.schema_resolver = schema_resolver

    def parse_file(self, path, sql):
        time.sleep(60)
        return ParsedFile(path=path, dialect=self.DIALECT)


class FailingParser:
    """Test parser that raises an exception."""

    DIALECT = "snowflake"

    def __init__(self, schema_resolver=None):
        self.schema_resolver = schema_resolver

    def parse_file(self, path, sql):
        raise ValueError("bad sql")


class ExitParser:
    """Test parser that exits without queueing result."""

    DIALECT = "snowflake"

    def __init__(self, schema_resolver=None):
        self.schema_resolver = schema_resolver

    def parse_file(self, path, sql):
        os._exit(0)


class TestSubprocessIsolate:
    """Test subprocess isolation for parser timeout handling."""

    def test_scenario_a_slow_parser_hard_killed_on_timeout(self):
        """Timeout properly kills a slow parser within timeout + 5 s."""
        indexer = Indexer()
        parser = SlowParser()
        path = Path("test_slow.sql")
        sql = "SELECT 1"

        start = time.time()
        result = indexer._index_single_file(parser, path, sql, timeout=2)
        elapsed = time.time() - start

        # Should complete in < 8 seconds (2 s timeout + 5 s join + overhead)
        assert elapsed < 8, f"Parser took {elapsed}s, expected hard kill < 8s"

        # Should return ParsedFile with timeout marker
        assert isinstance(result, ParsedFile)
        assert any("timeout:2s" in err for err in result.errors), (
            f"Expected timeout:2s marker, got {result.errors}"
        )

    def test_scenario_b_parsed_file_round_trips_through_queue(self):
        """ParsedFile round-trips cleanly through subprocess queue."""
        # Use a real parser on a simple SQL
        parser = SnowflakeParser(SchemaResolver())
        indexer = Indexer()
        path = Path("test.sql")
        sql = "SELECT 1"

        # Parse with timeout > expected time (should succeed normally)
        result = indexer._index_single_file(parser, path, sql, timeout=10)

        assert isinstance(result, ParsedFile)
        assert result.path == path
        assert result.dialect == "snowflake"
        assert len(result.statements) == 1

    def test_scenario_c_worker_exception_propagates_to_parent(self):
        """Exception in worker is caught and re-raised in parent."""
        indexer = Indexer()
        parser = FailingParser()

        with pytest.raises(ValueError, match="bad sql"):
            indexer._index_single_file(parser, Path("test.sql"), "BAD", timeout=5)

    def test_scenario_d_timeout_zero_bypasses_subprocess(self):
        """When timeout=0, parse runs in-process, no Process instantiation."""
        parser = SnowflakeParser(SchemaResolver())
        indexer = Indexer()
        path = Path("test.sql")
        sql = "SELECT 1"

        # Patch Process to raise if instantiated
        with patch("sqlcg.indexer.indexer.mp.get_context") as mock_ctx:
            mock_ctx.side_effect = RuntimeError("Process should not be created when timeout=0")
            # timeout=0 should bypass the subprocess path entirely
            result = indexer._index_single_file(parser, path, sql, timeout=0)

            # Should complete without calling get_context
            assert isinstance(result, ParsedFile)
            mock_ctx.assert_not_called()

    def test_scenario_e_subprocess_exit_without_queue_write_structured_error(self):
        """Subprocess exit without queueing produces structured error."""
        indexer = Indexer()
        parser = ExitParser()

        result = indexer._index_single_file(parser, Path("test.sql"), "SELECT 1", timeout=5)

        assert isinstance(result, ParsedFile)
        assert any("subprocess_empty_result" in err for err in result.errors), (
            f"Expected subprocess_empty_result marker, got {result.errors}"
        )

    def test_parsed_file_pickleable(self):
        """ParsedFile is pickleable for queue transport."""
        parser = SnowflakeParser(SchemaResolver())
        path = Path("test.sql")
        sql = "INSERT INTO tgt SELECT src.a FROM src"

        parsed = parser.parse_file(path, sql)

        # Should pickle without error
        pickled = pickle.dumps(parsed)
        unpickled = pickle.loads(pickled)

        assert unpickled.path == parsed.path
        assert unpickled.dialect == parsed.dialect
        assert len(unpickled.statements) == len(parsed.statements)
