"""Tests for the query-time bare-name fallback (PR-06 #26).

Covers the _bare_ref helper and the fallback behaviour in analyze upstream/downstream
and tools.trace_column_lineage when a qualified ref returns empty but the bare name
(schema stripped) yields results.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Scenario A — _bare_ref strips schema from 3-part ref
# ---------------------------------------------------------------------------


def test_scenario_a_bare_ref_strips_schema_from_3part_ref() -> None:
    """_bare_ref("mart.fact_t.amount") must return "fact_t.amount", not "amount".

    Using rsplit(".", 1)[-1] would yield "amount" (catastrophic false-positives).
    Using ".".join(parts[1:]) yields "fact_t.amount" (correct table.column form).
    """
    from sqlcg.cli.commands.analyze import _bare_ref

    result = _bare_ref("mart.fact_t.amount")
    assert result == "fact_t.amount", (
        f"_bare_ref('mart.fact_t.amount') must return 'fact_t.amount', got {result!r}. "
        "Using rsplit would yield only 'amount' — catastrophic multi-table false-positive."
    )


def test_scenario_a_bare_ref_strips_schema_tools() -> None:
    """tools._bare_ref must produce the same result as analyze._bare_ref."""
    from sqlcg.server.tools import _bare_ref as tools_bare_ref

    result = tools_bare_ref("mart.fact_t.amount")
    assert result == "fact_t.amount", (
        f"tools._bare_ref('mart.fact_t.amount') must return 'fact_t.amount', got {result!r}."
    )


# ---------------------------------------------------------------------------
# Scenario B — _bare_ref returns 2-part ref unchanged
# ---------------------------------------------------------------------------


def test_scenario_b_bare_ref_2part_unchanged() -> None:
    """_bare_ref("fact_t.amount") must return the ref unchanged (already bare)."""
    from sqlcg.cli.commands.analyze import _bare_ref

    result = _bare_ref("fact_t.amount")
    assert result == "fact_t.amount", (
        f"_bare_ref('fact_t.amount') must return 'fact_t.amount' unchanged, got {result!r}."
    )


def test_scenario_b_bare_ref_4part_strips_schema_only() -> None:
    """_bare_ref("wh.mart.fact_t.amount") strips only the leading schema."""
    from sqlcg.cli.commands.analyze import _bare_ref

    result = _bare_ref("wh.mart.fact_t.amount")
    assert result == "mart.fact_t.amount", (
        f"_bare_ref('wh.mart.fact_t.amount') must return 'mart.fact_t.amount', got {result!r}."
    )


# ---------------------------------------------------------------------------
# Scenario C — unqualified INSERT target: qualified analyze hits bare fallback
# ---------------------------------------------------------------------------


def test_scenario_c_upstream_fallback_fires_when_primary_empty() -> None:
    """analyze upstream fires bare-name fallback when the qualified ref returns empty.

    Simulates an INSERT INTO FACT_T (no schema) case: the graph only has
    "fact_t.amount" as a source node, but the user queries "mart.fact_t.amount".
    The fallback must find "fact_t.amount" results and print a hint.
    """
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from sqlcg.cli.main import app

    runner = CliRunner()

    # Primary query (qualified ref) returns empty; fallback (bare ref) returns results.
    bare_node_id = "mart.orders.amount"

    def mock_run_read(query: str, params: dict) -> list[dict]:
        # Qualified ref returns nothing; bare ref returns a result.
        if params.get("ref") == "mart.fact_t.amount":
            return []
        if params.get("bare") == "fact_t.amount":
            return [{"id": bare_node_id}]
        return []

    backend = MagicMock()
    backend.__enter__ = MagicMock(return_value=backend)
    backend.__exit__ = MagicMock(return_value=False)
    backend.run_read = MagicMock(side_effect=mock_run_read)

    with patch("sqlcg.cli.commands.analyze.get_backend", return_value=backend):
        result = runner.invoke(app, ["analyze", "upstream", "mart.fact_t.amount", "--raw"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    assert bare_node_id in result.output, (
        f"Fallback result {bare_node_id!r} must appear in output. Output: {result.output}"
    )
    assert "Hint:" in result.output, (
        f"Hint message must be printed when fallback fires. Output: {result.output}"
    )
    assert "fact_t.amount" in result.output, (
        f"Output must mention the bare name 'fact_t.amount'. Output: {result.output}"
    )


def test_scenario_c_downstream_fallback_fires_when_primary_empty() -> None:
    """analyze downstream fires bare-name fallback analogous to upstream."""
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from sqlcg.cli.main import app

    runner = CliRunner()
    bare_node_id = "mart.report.col"

    def mock_run_read(query: str, params: dict) -> list[dict]:
        if params.get("ref") == "mart.fact_t.amount":
            return []
        if params.get("bare") == "fact_t.amount":
            return [{"id": bare_node_id}]
        return []

    backend = MagicMock()
    backend.__enter__ = MagicMock(return_value=backend)
    backend.__exit__ = MagicMock(return_value=False)
    backend.run_read = MagicMock(side_effect=mock_run_read)

    with patch("sqlcg.cli.commands.analyze.get_backend", return_value=backend):
        result = runner.invoke(app, ["analyze", "downstream", "mart.fact_t.amount", "--raw"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    assert bare_node_id in result.output, (
        f"Fallback result {bare_node_id!r} must appear in downstream output. "
        f"Output: {result.output}"
    )
    assert "Hint:" in result.output, (
        f"Hint message must be printed when downstream fallback fires. Output: {result.output}"
    )


# ---------------------------------------------------------------------------
# Scenario D — schema-qualified path does NOT fire the fallback
# ---------------------------------------------------------------------------


def test_scenario_d_no_fallback_when_primary_succeeds() -> None:
    """Fallback must NOT fire when the primary (qualified) query returns results.

    When the INSERT target was properly indexed as "mart.fact_t", the primary
    query succeeds and the bare-name fallback block must not execute.
    """
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from sqlcg.cli.main import app

    runner = CliRunner()
    primary_node_id = "mart.orders.amount"

    call_log: list[dict] = []

    def mock_run_read(query: str, params: dict) -> list[dict]:
        call_log.append(dict(params))
        if params.get("ref") == "mart.fact_t.amount":
            return [{"id": primary_node_id}]
        return []

    backend = MagicMock()
    backend.__enter__ = MagicMock(return_value=backend)
    backend.__exit__ = MagicMock(return_value=False)
    backend.run_read = MagicMock(side_effect=mock_run_read)

    with patch("sqlcg.cli.commands.analyze.get_backend", return_value=backend):
        result = runner.invoke(app, ["analyze", "upstream", "mart.fact_t.amount", "--raw"])

    assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
    assert primary_node_id in result.output, (
        f"Primary result {primary_node_id!r} must appear in output. Output: {result.output}"
    )
    # Fallback must not fire — no "bare" param query should have been issued
    bare_calls = [c for c in call_log if "bare" in c]
    assert not bare_calls, (
        f"Bare-name fallback must NOT fire when primary query succeeds. "
        f"But backend was called with bare param: {bare_calls}. Output: {result.output}"
    )
    assert "Hint:" not in result.output, (
        f"Hint must not appear when primary query succeeds. Output: {result.output}"
    )


# ---------------------------------------------------------------------------
# Guard: _bare_ref never uses rsplit (catastrophic match guard)
# ---------------------------------------------------------------------------


def test_bare_ref_does_not_use_rsplit_semantics() -> None:
    """The rsplit variant would yield only the column name, matching every column.

    For "mart.fact_t.amount":
      - WRONG: rsplit(".", 1)[-1] → "amount" (matches every column named amount)
      - CORRECT: ".".join(parts[1:]) → "fact_t.amount" (table-qualified)
    """
    from sqlcg.cli.commands.analyze import _bare_ref

    result = _bare_ref("mart.fact_t.amount")
    assert result != "amount", (
        "rsplit-style derivation 'amount' must never be returned — "
        "it would match every column named 'amount' in any table. "
        f"Got {result!r}"
    )
    assert "." in result, (
        f"_bare_ref result must be table-qualified (contain a dot), got {result!r}"
    )
