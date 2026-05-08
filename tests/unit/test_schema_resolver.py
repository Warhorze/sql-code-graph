"""Unit tests for SchemaResolver."""

import threading

import pytest

from sqlcg.lineage.schema_resolver import SchemaResolver


class TestSchemaResolver:
    """Test SchemaResolver functionality."""

    def test_init_creates_empty_schema(self):
        """Test initialization creates empty schema."""
        resolver = SchemaResolver(dialect="snowflake")
        assert resolver.dialect == "snowflake"
        assert resolver.as_dict() == {}

    def test_cache_cleared_after_add_create_table(self):
        """Test that cache is cleared when adding a table."""
        import sqlglot

        resolver = SchemaResolver()

        # Parse a real CREATE TABLE statement
        create_ast = sqlglot.parse("CREATE TABLE public.orders (id INT, amount DECIMAL);")[0]

        # Prime the cache
        _ = resolver.as_dict()
        assert resolver._cache is not None, "Cache should be populated"

        # Add a table and verify cache was cleared
        resolver.add_create_table(create_ast)
        assert resolver._cache is None, "Cache should be cleared after add_create_table"

        # Verify the table was actually added
        result = resolver.as_dict()
        assert "public" in result
        assert "orders" in result["public"]

    def test_concurrent_add_and_as_dict(self):
        """Test concurrent add_create_table and as_dict calls don't corrupt state.

        Uses threading.Barrier to synchronize thread execution.
        """
        import sqlglot

        resolver = SchemaResolver()
        barrier = threading.Barrier(2)
        results = []

        def adder():
            create_ast = sqlglot.parse("CREATE TABLE public.users (id INT);")[0]
            barrier.wait()  # Synchronize
            resolver.add_create_table(create_ast)

        def reader():
            barrier.wait()  # Synchronize
            result = resolver.as_dict()
            results.append(result)

        t1 = threading.Thread(target=adder)
        t2 = threading.Thread(target=reader)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Verify no exception was raised and results are valid dicts
        assert len(results) == 1
        assert isinstance(results[0], dict)

    def test_add_information_schema_raises_not_implemented(self):
        """Test that add_information_schema raises NotImplementedError."""
        resolver = SchemaResolver()
        with pytest.raises(
            NotImplementedError, match="--schema-from-info-schema is not yet implemented"
        ):
            resolver.add_information_schema("dummy.csv")

    def test_as_dict_returns_nested_structure(self):
        """Test as_dict returns properly nested structure."""
        import sqlglot

        resolver = SchemaResolver()

        # Add tables with different catalogs/dbs
        create_ast1 = sqlglot.parse("CREATE TABLE schema1.users (id INT);")[0]
        create_ast2 = sqlglot.parse("CREATE TABLE schema1.orders (user_id INT);")[0]

        resolver.add_create_table(create_ast1)
        resolver.add_create_table(create_ast2)

        result = resolver.as_dict()
        assert "schema1" in result
        assert "users" in result["schema1"]
        assert "orders" in result["schema1"]
        assert result["schema1"]["users"] == ["id"]
        assert result["schema1"]["orders"] == ["user_id"]

    def test_add_view_sources(self):
        """Test adding view sources."""
        from pathlib import Path

        from sqlcg.parsers import ParsedFile

        resolver = SchemaResolver()

        view_sources = {
            "view1": ParsedFile(path=Path("view1.sql")),
            "view2": ParsedFile(path=Path("view2.sql")),
        }

        resolver.add_view_sources(view_sources)

        # Verify cache was invalidated
        assert resolver._cache is None

        # Verify views were stored
        assert len(resolver._view_bodies) == 2
        assert "view1" in resolver._view_bodies
        assert "view2" in resolver._view_bodies

    def test_as_dict_returns_deep_copy_not_reference(self):
        """Test that as_dict() returns a deep copy, not a reference to internal cache.

        Ensures that mutations to the returned dict do not corrupt the internal state.
        """
        import sqlglot

        resolver = SchemaResolver()

        # Add a table
        create_ast = sqlglot.parse("CREATE TABLE public.users (id INT);")[0]
        resolver.add_create_table(create_ast)

        # Get the dict
        dict1 = resolver.as_dict()
        assert "public" in dict1

        # Mutate the returned dict
        dict1["public"]["users"] = ["id", "corrupted"]
        dict1["newkey"] = {"fake": "data"}

        # Get it again
        dict2 = resolver.as_dict()

        # Verify the internal cache is unaffected
        assert dict2["public"]["users"] == ["id"]
        assert "newkey" not in dict2

    # -----------------------------------------------------------------------
    # T-08 — add_information_schema
    # -----------------------------------------------------------------------

    @pytest.mark.xfail(reason="add_information_schema not yet implemented — T-08", strict=True)
    def test_add_information_schema_populates_tables(self, tmp_path):
        """CSV with 2 tables / 3 columns each must populate resolver in ORDINAL_POSITION order."""
        csv_file = tmp_path / "cols.csv"
        csv_file.write_text(
            "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
            "mydb,BA,orders,total,2,DECIMAL\n"
            "mydb,BA,orders,id,1,INT\n"
            "mydb,BA,orders,status,3,STRING\n"
            "mydb,BA,customers,id,1,INT\n"
            "mydb,BA,customers,name,2,STRING\n"
            "mydb,BA,customers,email,3,STRING\n",
            encoding="utf-8",
        )
        resolver = SchemaResolver()
        result = resolver.add_information_schema(csv_file)

        assert result == 2, f"Expected 2 tables loaded, got {result}"

        schema = resolver.as_dict()
        assert "BA" in schema
        assert "orders" in schema["BA"]
        assert "customers" in schema["BA"]
        # Columns must be sorted by ORDINAL_POSITION, not CSV row order
        assert schema["BA"]["orders"] == ["id", "total", "status"]
        assert schema["BA"]["customers"] == ["id", "name", "email"]

    @pytest.mark.xfail(reason="add_information_schema not yet implemented — T-08", strict=True)
    def test_add_information_schema_missing_column_raises(self, tmp_path):
        """CSV missing ORDINAL_POSITION must raise ValueError naming the missing column."""
        csv_file = tmp_path / "bad.csv"
        csv_file.write_text(
            "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME\n"
            "mydb,BA,orders,id\n",
            encoding="utf-8",
        )
        resolver = SchemaResolver()
        with pytest.raises(ValueError, match="ORDINAL_POSITION"):
            resolver.add_information_schema(csv_file)

    @pytest.mark.xfail(reason="add_information_schema not yet implemented — T-08", strict=True)
    def test_add_information_schema_returns_table_count(self, tmp_path):
        """Return value must be the number of distinct tables, not row count."""
        csv_file = tmp_path / "cols.csv"
        csv_file.write_text(
            "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
            "mydb,BA,src,a,1,INT\n"
            "mydb,BA,src,b,2,INT\n"
            "mydb,BA,tgt,x,1,INT\n",
            encoding="utf-8",
        )
        resolver = SchemaResolver()
        assert resolver.add_information_schema(csv_file) == 2

    @pytest.mark.xfail(reason="add_information_schema not yet implemented — T-08", strict=True)
    def test_add_information_schema_idempotent(self, tmp_path):
        """Calling add_information_schema twice must not duplicate columns."""
        csv_file = tmp_path / "cols.csv"
        csv_file.write_text(
            "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE\n"
            "mydb,BA,src,a,1,INT\n",
            encoding="utf-8",
        )
        resolver = SchemaResolver()
        resolver.add_information_schema(csv_file)
        resolver.add_information_schema(csv_file)

        schema = resolver.as_dict()
        assert schema["BA"]["src"] == ["a"], (
            "Second add_information_schema call must not duplicate columns"
        )
