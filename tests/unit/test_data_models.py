"""Unit tests for data models."""

import pytest

from sqlcg.parsers import ColumnRef, LineageEdge, ParsedFile, QueryNode, TableRef


class TestTableRef:
    """Test TableRef data model."""

    def test_table_ref_full_id_with_all_parts(self):
        """Test full_id with catalog, db, and name."""
        table = TableRef(catalog="catalog", db="schema", name="table")
        assert table.full_id == "catalog.schema.table"

    def test_table_ref_full_id_without_catalog(self):
        """Test full_id without catalog."""
        table = TableRef(db="schema", name="table")
        assert table.full_id == "schema.table"

    def test_table_ref_full_id_name_only(self):
        """Test full_id with name only."""
        table = TableRef(name="table")
        assert table.full_id == "table"

    def test_table_ref_full_id_collision_prevention(self):
        """Test that different catalogs produce different full_ids."""
        # This is finding 5.4: Multi-catalog collision prevention
        table1 = TableRef(catalog="catalog1", db="schema", name="table")
        table2 = TableRef(catalog="catalog2", db="schema", name="table")

        assert table1.full_id != table2.full_id
        assert table1.full_id == "catalog1.schema.table"
        assert table2.full_id == "catalog2.schema.table"

    def test_table_ref_frozen(self):
        """Test that TableRef is frozen."""
        table = TableRef(name="table")
        with pytest.raises(AttributeError):
            table.name = "new_table"

    def test_table_ref_alias(self):
        """Test TableRef with alias."""
        table = TableRef(name="table", alias="t")
        assert table.name == "table"
        assert table.alias == "t"


class TestColumnRef:
    """Test ColumnRef data model."""

    def test_column_ref_full_id(self):
        """Test ColumnRef full_id."""
        table = TableRef(db="schema", name="table")
        col = ColumnRef(table=table, name="column")
        assert col.full_id == "schema.table.column"

    def test_column_ref_full_id_with_catalog(self):
        """Test ColumnRef full_id with catalog."""
        table = TableRef(catalog="catalog", db="schema", name="table")
        col = ColumnRef(table=table, name="column")
        assert col.full_id == "catalog.schema.table.column"

    def test_column_ref_frozen(self):
        """Test that ColumnRef is frozen."""
        table = TableRef(name="table")
        col = ColumnRef(table=table, name="column")
        with pytest.raises(AttributeError):
            col.name = "new_column"


class TestLineageEdge:
    """Test LineageEdge data model."""

    def test_lineage_edge_frozen(self):
        """Test that LineageEdge is frozen."""
        table = TableRef(name="table")
        col1 = ColumnRef(table=table, name="col1")
        col2 = ColumnRef(table=table, name="col2")
        edge = LineageEdge(src=col1, dst=col2)

        with pytest.raises(AttributeError):
            edge.transform = "MODIFIED"

    def test_lineage_edge_hashable(self):
        """Test that LineageEdge is hashable."""
        table = TableRef(name="table")
        col1 = ColumnRef(table=table, name="col1")
        col2 = ColumnRef(table=table, name="col2")

        edge1 = LineageEdge(src=col1, dst=col2, transform="PASS_THROUGH")
        edge2 = LineageEdge(src=col1, dst=col2, transform="PASS_THROUGH")

        # Same edges should have same hash
        assert hash(edge1) == hash(edge2)

        # Can be used in sets and dicts
        edge_set = {edge1, edge2}
        assert len(edge_set) == 1

    def test_lineage_edge_equality(self):
        """Test LineageEdge equality."""
        table = TableRef(name="table")
        col1 = ColumnRef(table=table, name="col1")
        col2 = ColumnRef(table=table, name="col2")

        edge1 = LineageEdge(src=col1, dst=col2, confidence=0.9)
        edge2 = LineageEdge(src=col1, dst=col2, confidence=0.9)

        assert edge1 == edge2

    def test_lineage_edge_inequality(self):
        """Test LineageEdge inequality."""
        table = TableRef(name="table")
        col1 = ColumnRef(table=table, name="col1")
        col2 = ColumnRef(table=table, name="col2")

        edge1 = LineageEdge(src=col1, dst=col2, confidence=0.9)
        edge2 = LineageEdge(src=col1, dst=col2, confidence=0.8)

        assert edge1 != edge2


class TestQueryNode:
    """Test QueryNode data model."""

    def test_query_node_mutable(self):
        """Test that QueryNode is mutable."""
        from pathlib import Path

        node = QueryNode(file=Path("/test.sql"), statement_index=0, sql="SELECT 1")
        assert node.kind == ""

        # Should be able to mutate fields
        node.kind = "SELECT"
        assert node.kind == "SELECT"

    def test_query_node_defaults(self):
        """Test QueryNode default values."""
        from pathlib import Path

        node = QueryNode(file=Path("/test.sql"), statement_index=0, sql="SELECT 1")
        assert node.parse_failed is False
        assert node.confidence == 1.0
        assert node.sources == []
        assert node.column_lineage == []


class TestParsedFile:
    """Test ParsedFile data model."""

    def test_parsed_file_initialization(self):
        """Test ParsedFile initialization."""
        from pathlib import Path

        pf = ParsedFile(path=Path("/test.sql"), dialect="ansi")
        assert pf.path == Path("/test.sql")
        assert pf.dialect == "ansi"
        assert pf.statements == []
        assert pf.errors == []

    def test_parsed_file_mutable(self):
        """Test that ParsedFile is mutable."""
        from pathlib import Path

        pf = ParsedFile(path=Path("/test.sql"))
        assert pf.dialect is None

        pf.dialect = "snowflake"
        assert pf.dialect == "snowflake"
