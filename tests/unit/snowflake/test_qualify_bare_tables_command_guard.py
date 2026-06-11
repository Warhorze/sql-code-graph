"""Unit tests for _qualify_bare_tables Command-node guard (PR 2, step 2.1).

The crash path: a file beginning with ``USE SCHEMA da;`` followed by an
unsupported statement like ``ALTER EXTERNAL TABLE da.x REFRESH;`` causes
sqlglot to emit an ``exp.Command`` fallback node.  The Command node's
``.expression`` attribute is a plain *str*, not an ``exp.Expression``; calling
``.args`` on a str raises ``AttributeError: 'str' object has no attribute
'args'``.  Without the fix the error propagates out of ``_qualify_bare_tables``
and nukes every statement in the file.

Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.1.
"""

from __future__ import annotations

from pathlib import Path

import sqlglot.expressions as exp

from sqlcg.lineage.schema_resolver import SchemaResolver
from sqlcg.parsers.snowflake_parser import SnowflakeParser


def _make_parser() -> SnowflakeParser:
    return SnowflakeParser(SchemaResolver(dialect="snowflake"))


# ---------------------------------------------------------------------------
# Crash-reproduction fixture
# ---------------------------------------------------------------------------


def test_parse_file_command_node_does_not_raise():
    """parse_file returns a ParsedFile with >=1 statement on the crash-pattern fixture.

    Pre-fix: ``USE SCHEMA da;`` followed by ``ALTER EXTERNAL TABLE da.x REFRESH;``
    raised ``AttributeError: 'str' object has no attribute 'args'``.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.1 (N2: crash-fixture gate).
    """
    sql = "USE SCHEMA da;\nALTER EXTERNAL TABLE da.x REFRESH;"
    parser = _make_parser()
    result = parser.parse_file(Path("crash_fixture.sql"), sql)

    # Must not raise — if it did the test would error before this line.
    # Must yield at least the statement produced from the USE SCHEMA context.
    assert len(result.statements) >= 1, (
        f"Expected >=1 statement from crash-pattern fixture, got {len(result.statements)}"
    )


def test_parse_file_call_stmt_does_not_raise():
    """CALL statements (another Command source) do not crash _qualify_bare_tables."""
    sql = "USE SCHEMA da;\nCALL my_proc();"
    parser = _make_parser()
    # Must not raise
    result = parser.parse_file(Path("call_fixture.sql"), sql)
    assert result is not None


def test_parse_file_put_remove_does_not_raise():
    """PUT / REMOVE statements (Command fallback) do not crash _qualify_bare_tables."""
    sql = "USE SCHEMA da;\nPUT file://local.csv @my_stage;\nREMOVE @my_stage/local.csv;"
    parser = _make_parser()
    result = parser.parse_file(Path("put_remove_fixture.sql"), sql)
    assert result is not None


# ---------------------------------------------------------------------------
# _qualify_bare_tables is a no-op on a Command node
# ---------------------------------------------------------------------------


def test_qualify_bare_tables_noop_on_command_node():
    """_qualify_bare_tables neither raises nor qualifies tables on a Command node.

    A pure no-op: the Command node comes in, the method returns without touching
    it, and no exp.Table nodes are fabricated.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.1.
    """
    import sqlglot

    stmts = sqlglot.parse("ALTER EXTERNAL TABLE x REFRESH", dialect="snowflake")
    assert stmts, "sqlglot should produce at least one node"
    cmd = stmts[0]
    # Confirm sqlglot did produce a Command node for this unsupported syntax.
    assert isinstance(cmd, exp.Command), f"Expected exp.Command, got {type(cmd).__name__}"

    # Must not raise, must not add table nodes.
    before_tables = list(cmd.find_all(exp.Table))
    SnowflakeParser._qualify_bare_tables(cmd, "da")
    after_tables = list(cmd.find_all(exp.Table))

    assert before_tables == after_tables, (
        "Command node table list should be unchanged after _qualify_bare_tables"
    )


# ---------------------------------------------------------------------------
# Normal qualification still works
# ---------------------------------------------------------------------------


def test_qualify_bare_tables_still_qualifies_select():
    """_qualify_bare_tables qualifies bare tables in a normal SELECT.

    Ensures the isinstance guard does not break the happy path for real
    exp.Expression nodes.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.1.
    """
    import sqlglot

    stmts = sqlglot.parse("SELECT * FROM my_table", dialect="snowflake")
    assert stmts
    stmt = stmts[0]

    SnowflakeParser._qualify_bare_tables(stmt, "myschema")

    tables = list(stmt.find_all(exp.Table))
    qualified = [t for t in tables if t.args.get("db")]
    assert qualified, "Expected at least one table to be schema-qualified"
    assert qualified[0].args["db"].name == "myschema", (
        f"Expected db='myschema', got {qualified[0].args['db'].name!r}"
    )


def test_qualify_bare_tables_still_qualifies_insert():
    """_qualify_bare_tables qualifies bare tables in a normal INSERT … SELECT.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.1.
    """
    import sqlglot

    stmts = sqlglot.parse(
        "INSERT INTO tgt SELECT col FROM src",
        dialect="snowflake",
    )
    assert stmts
    stmt = stmts[0]

    SnowflakeParser._qualify_bare_tables(stmt, "myschema")

    tables = list(stmt.find_all(exp.Table))
    qualified = [t for t in tables if t.args.get("db")]
    assert len(qualified) == 2, (  # tgt and src
        f"Expected both tgt and src to be qualified, got {[t.name for t in qualified]}"
    )


# ---------------------------------------------------------------------------
# Command.find_all safety (outer loop in _qualify_bare_tables)
# ---------------------------------------------------------------------------


def test_command_find_all_table_is_safe_and_empty():
    """exp.Command.find_all(exp.Table) does not raise and yields no exp.Table nodes.

    This is the outer ``stmt.find_all(exp.Table)`` loop in ``_qualify_bare_tables``.
    The inner candidate loop is guarded; this confirms the outer loop is also safe.

    Guards plan/sprints/sprint_postmortem_fixes.md §PR-2 Step 2.1.
    """
    import sqlglot

    stmts = sqlglot.parse("ALTER EXTERNAL TABLE x REFRESH", dialect="snowflake")
    assert stmts
    cmd = stmts[0]
    assert isinstance(cmd, exp.Command)

    tables = list(cmd.find_all(exp.Table))
    assert tables == [], f"Expected no exp.Table from Command.find_all, got {tables}"
