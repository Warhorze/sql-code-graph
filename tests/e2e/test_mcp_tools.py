"""E2E tests for MCP tools."""

import tempfile
from pathlib import Path

import pytest

from sqlcg.server.exceptions import NotIndexedError
from sqlcg.server.tools import (
    execute_cypher,
    find_table_usages,
    get_downstream_dependencies,
    get_upstream_dependencies,
    index_repo,
    init_backend,
    list_dialects_and_repos,
    search_sql_pattern,
    trace_column_lineage,
)


@pytest.fixture
def indexed_graph(tmp_path):
    """Index synthetic fixtures into a temporary test database.

    Creates a fresh backend and pre-indexes the synthetic fixtures
    for each test that uses this fixture.
    """
    db_path = str(tmp_path / "test.db")
    init_backend(db_path)

    # Index synthetic fixtures
    result = index_repo(str(Path(__file__).parent.parent / "fixtures" / "synthetic"))
    assert result["files_parsed"] > 0
    assert result["tables_found"] > 0

    yield db_path


class TestIndexRepo:
    """Tests for index_repo tool."""

    def test_index_repo_returns_summary_dict(self, tmp_path):
        """Test that index_repo returns expected summary dict."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        fixture_path = str(Path(__file__).parent.parent / "fixtures" / "synthetic")
        result = index_repo(fixture_path, dialect="ansi")

        assert isinstance(result, dict)
        assert "files_parsed" in result
        assert "parse_errors" in result
        assert "tables_found" in result
        assert "lineage_edges_created" in result
        assert result["files_parsed"] > 0

    def test_index_repo_nonexistent_path_raises(self, tmp_path):
        """Test that index_repo raises for nonexistent path."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(ValueError, match="does not exist"):
            index_repo("/nonexistent/path")

    def test_index_repo_file_path_raises(self, tmp_path):
        """Test that index_repo raises for file path instead of directory."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with tempfile.NamedTemporaryFile(suffix=".sql") as f:
            with pytest.raises(ValueError, match="is not a directory"):
                index_repo(f.name)


class TestTraceColumnLineage:
    """Tests for trace_column_lineage tool."""

    def test_trace_column_lineage_requires_indexed_graph(self, tmp_path):
        """Test that trace_column_lineage raises NotIndexedError on empty graph."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(NotIndexedError, match="No repos have been indexed"):
            trace_column_lineage("orders.id")

    def test_trace_column_lineage_invalid_format_raises(self, indexed_graph):
        """Test that invalid column reference format raises InvalidColumnRefError."""
        from sqlcg.server.exceptions import InvalidColumnRefError

        # Try with just a single name (no dot)
        with pytest.raises(InvalidColumnRefError):
            trace_column_lineage("invalid_format")

    def test_trace_column_lineage_returns_result(self, indexed_graph):
        """Test that trace_column_lineage returns LineageResult."""
        result = trace_column_lineage("orders.id", max_depth=3)
        assert result.column == "orders.id"
        assert isinstance(result.lineage, list)


class TestFindTableUsages:
    """Tests for find_table_usages tool."""

    def test_find_table_usages_requires_indexed_graph(self, tmp_path):
        """Test that find_table_usages raises NotIndexedError on empty graph."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(NotIndexedError, match="No repos have been indexed"):
            find_table_usages("orders")

    def test_find_table_usages_returns_result(self, indexed_graph):
        """Test that find_table_usages returns TableUsageResult."""
        result = find_table_usages("orders")
        assert result.table == "orders"
        assert isinstance(result.usages, list)


class TestGetDownstreamDependencies:
    """Tests for get_downstream_dependencies tool."""

    def test_get_downstream_dependencies_requires_indexed_graph(self, tmp_path):
        """Test that get_downstream_dependencies raises NotIndexedError on empty graph."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(NotIndexedError, match="No repos have been indexed"):
            get_downstream_dependencies("orders.id")

    def test_get_downstream_dependencies_returns_result(self, indexed_graph):
        """Test that get_downstream_dependencies returns DependencyResult."""
        result = get_downstream_dependencies("orders.id", max_depth=3)
        assert result.root == "orders.id"
        assert isinstance(result.nodes, list)


class TestGetUpstreamDependencies:
    """Tests for get_upstream_dependencies tool."""

    def test_get_upstream_dependencies_requires_indexed_graph(self, tmp_path):
        """Test that get_upstream_dependencies raises NotIndexedError on empty graph."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(NotIndexedError, match="No repos have been indexed"):
            get_upstream_dependencies("orders.id")

    def test_get_upstream_dependencies_returns_result(self, indexed_graph):
        """Test that get_upstream_dependencies returns DependencyResult."""
        result = get_upstream_dependencies("orders.id", max_depth=3)
        assert result.root == "orders.id"
        assert isinstance(result.nodes, list)


class TestSearchSqlPattern:
    """Tests for search_sql_pattern tool."""

    def test_search_sql_pattern_requires_indexed_graph(self, tmp_path):
        """Test that search_sql_pattern raises NotIndexedError on empty graph."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(NotIndexedError, match="No repos have been indexed"):
            search_sql_pattern("SELECT")

    def test_search_sql_pattern_returns_result(self, indexed_graph):
        """Test that search_sql_pattern returns SqlPatternResult."""
        result = search_sql_pattern("SELECT", limit=10)
        assert result.pattern == "SELECT"
        assert isinstance(result.matches, list)


class TestListDialectsAndRepos:
    """Tests for list_dialects_and_repos tool."""

    def test_list_dialects_and_repos_requires_indexed_graph(self, tmp_path):
        """Test that list_dialects_and_repos raises NotIndexedError on empty graph."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(NotIndexedError, match="No repos have been indexed"):
            list_dialects_and_repos()

    def test_list_dialects_and_repos_returns_result(self, indexed_graph):
        """Test that list_dialects_and_repos returns DialectRepoResult."""
        result = list_dialects_and_repos()
        assert isinstance(result.repos, list)


class TestExecuteCypher:
    """Tests for execute_cypher tool."""

    def test_execute_cypher_read_only_query_succeeds(self, indexed_graph):
        """Test that execute_cypher executes read-only queries."""
        result = execute_cypher("MATCH (n:Repo) RETURN n.path AS path LIMIT 10")
        assert isinstance(result, list)

    def test_execute_cypher_blocklist_rejects_create(self, tmp_path):
        """Test that execute_cypher rejects CREATE operations."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(ValueError, match="Write operations are not permitted"):
            execute_cypher("CREATE NODE TABLE Test (id STRING PRIMARY KEY)")

    def test_execute_cypher_blocklist_rejects_merge(self, tmp_path):
        """Test that execute_cypher rejects MERGE operations."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(ValueError, match="Write operations are not permitted"):
            execute_cypher("MERGE (n:Test {id: 1})")

    def test_execute_cypher_blocklist_rejects_delete(self, tmp_path):
        """Test that execute_cypher rejects DELETE operations."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(ValueError, match="Write operations are not permitted"):
            execute_cypher("MATCH (n:Test) DELETE n")

    def test_execute_cypher_blocklist_rejects_set(self, tmp_path):
        """Test that execute_cypher rejects SET operations."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(ValueError, match="Write operations are not permitted"):
            execute_cypher("MATCH (n:Test) SET n.field = 'value'")

    def test_execute_cypher_blocklist_rejects_drop(self, tmp_path):
        """Test that execute_cypher rejects DROP operations."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(ValueError, match="Write operations are not permitted"):
            execute_cypher("DROP TABLE Test")

    def test_execute_cypher_blocklist_rejects_truncate(self, tmp_path):
        """Test that execute_cypher rejects TRUNCATE operations."""
        db_path = str(tmp_path / "test.db")
        init_backend(db_path)
        with pytest.raises(ValueError, match="Write operations are not permitted"):
            execute_cypher("TRUNCATE TABLE Test")

    def test_execute_cypher_string_literal_bypasses_blocklist(self, indexed_graph):
        """Test that mutation keywords inside string literals are stripped.

        A query with 'DROP TABLE' in a string literal should succeed
        because the blocklist check strips string literals first.
        """
        # This query looks for nodes where the property contains DROP TABLE
        result = execute_cypher(
            "MATCH (n:SqlQuery) WHERE contains(n.sql, 'DROP TABLE') RETURN n LIMIT 1"
        )
        assert isinstance(result, list)

    def test_execute_cypher_auto_limit_appended(self, indexed_graph):
        """Test that LIMIT is auto-appended if missing."""
        # This should not raise even though there's no LIMIT
        result = execute_cypher("MATCH (n:Repo) RETURN n")
        assert isinstance(result, list)
        # Result should be limited to 500
        assert len(result) <= 500

    def test_execute_cypher_existing_limit_not_duplicated(self, indexed_graph):
        """Test that existing LIMIT is not duplicated."""
        result = execute_cypher("MATCH (n:Repo) RETURN n LIMIT 5")
        assert isinstance(result, list)
        assert len(result) <= 5

    def test_execute_cypher_limit_not_fooled_by_string_literal(self, indexed_graph):
        """Query with 'limit' in a string literal should still get LIMIT 500 appended.

        Regression test for C1: the LIMIT check must use stripped query, not original.
        A query like WHERE x = 'limit test' should still get LIMIT 500 appended.
        """
        result = execute_cypher("MATCH (n:SqlQuery) WHERE contains(n.sql, 'limit test') RETURN n")
        assert isinstance(result, list)
