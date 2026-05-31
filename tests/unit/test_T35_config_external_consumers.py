"""Failing acceptance tests for #35 PR-1 — get_external_consumers config reader.

These tests MUST FAIL until the developer implements:
  - ExternalConsumerSpec model in config.py
  - get_external_consumers(path) -> list[ExternalConsumerSpec] in config.py

Tests are named T35-CFG-* per the plan's test strategy.
"""

import tempfile
from pathlib import Path

import pytest

# ExternalConsumerSpec and get_external_consumers are introduced by #35 PR-1.
# Import with skip guard so the suite does not hard-error before the feature lands.
try:
    from sqlcg.core.config import (  # noqa: F401  # introduced by T35
        ExternalConsumerSpec,
        get_external_consumers,
    )

    _SYMBOLS_AVAILABLE = True
except (ImportError, AttributeError):
    _SYMBOLS_AVAILABLE = False


@pytest.fixture
def tmp_path_fixture():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ---------------------------------------------------------------------------
# T35-CFG-1: two [[sqlcg.external_consumers]] tables → two specs, values lowercased
# ---------------------------------------------------------------------------


def test_T35_CFG_1_two_consumers_parsed(tmp_path_fixture):
    """T35-CFG-1: two [[sqlcg.external_consumers]] tables yield two ExternalConsumerSpec
    objects with lowercased consumer_type and consumes entries.
    """
    if not _SYMBOLS_AVAILABLE:
        pytest.skip("ExternalConsumerSpec / get_external_consumers not yet implemented (#35)")

    toml = (
        "[[sqlcg.external_consumers]]\n"
        'name = "Tableau: Sales Dashboard"\n'
        'kind = "TABLEAU"\n'
        'consumes = ["IA_Sales.Fct_Orders", "IA_Sales.Dim_Customer"]\n'
        "\n"
        "[[sqlcg.external_consumers]]\n"
        'name = "Reverse-ETL: HubSpot sync"\n'
        'kind = "REVERSE_ETL"\n'
        'consumes = ["IA_Marketing.Audience_Export"]\n'
    )
    (tmp_path_fixture / ".sqlcg.toml").write_text(toml)

    specs = get_external_consumers(tmp_path_fixture)

    assert len(specs) == 2, f"Expected 2 specs; got {len(specs)}: {specs}"

    tableau = next((s for s in specs if "Tableau" in s.name), None)
    assert tableau is not None, "Expected a spec with name containing 'Tableau'"
    assert tableau.consumer_type == "tableau", (
        f"consumer_type must be lowercased; got {tableau.consumer_type!r}"
    )
    assert "ia_sales.fct_orders" in tableau.consumes, (
        f"consumes entries must be lowercased; got {tableau.consumes}"
    )
    assert "ia_sales.dim_customer" in tableau.consumes, (
        f"consumes entries must be lowercased; got {tableau.consumes}"
    )

    hubspot = next((s for s in specs if "HubSpot" in s.name), None)
    assert hubspot is not None, "Expected a spec with name containing 'HubSpot'"
    assert hubspot.consumer_type == "reverse_etl", (
        f"consumer_type must be lowercased; got {hubspot.consumer_type!r}"
    )
    assert "ia_marketing.audience_export" in hubspot.consumes, (
        f"consumes entries must be lowercased; got {hubspot.consumes}"
    )


# ---------------------------------------------------------------------------
# T35-CFG-2a: absent section → empty list, no exception
# ---------------------------------------------------------------------------


def test_T35_CFG_2a_absent_section_returns_empty(tmp_path_fixture):
    """T35-CFG-2a: when [[sqlcg.external_consumers]] is absent, returns []."""
    if not _SYMBOLS_AVAILABLE:
        pytest.skip("get_external_consumers not yet implemented (#35)")

    (tmp_path_fixture / ".sqlcg.toml").write_text('[sqlcg]\ndialect = "snowflake"\n')

    specs = get_external_consumers(tmp_path_fixture)

    assert specs == [], f"Expected [] when section is absent; got {specs}"


def test_T35_CFG_2a_no_config_file_returns_empty(tmp_path_fixture):
    """T35-CFG-2a: when .sqlcg.toml does not exist, returns []."""
    if not _SYMBOLS_AVAILABLE:
        pytest.skip("get_external_consumers not yet implemented (#35)")

    specs = get_external_consumers(tmp_path_fixture)

    assert specs == [], f"Expected [] with no config file; got {specs}"


# ---------------------------------------------------------------------------
# T35-CFG-2b: malformed TOML (string instead of array) → [], no exception
# ---------------------------------------------------------------------------


def test_T35_CFG_2b_malformed_toml_returns_empty(tmp_path_fixture):
    """T35-CFG-2b: malformed TOML that would cause a parse error returns [] without raising."""
    if not _SYMBOLS_AVAILABLE:
        pytest.skip("get_external_consumers not yet implemented (#35)")

    (tmp_path_fixture / ".sqlcg.toml").write_text("this is not valid toml {{{")

    # Must not raise — returns empty list
    specs = get_external_consumers(tmp_path_fixture)
    assert specs == [], f"Expected [] on malformed TOML; got {specs}"


# ---------------------------------------------------------------------------
# T35-CFG-2c: row missing `name` is skipped; row with empty `consumes` is skipped
# ---------------------------------------------------------------------------


def test_T35_CFG_2c_row_missing_name_skipped(tmp_path_fixture):
    """T35-CFG-2c: a row without a `name` key is silently skipped."""
    if not _SYMBOLS_AVAILABLE:
        pytest.skip("get_external_consumers not yet implemented (#35)")

    toml = (
        "[[sqlcg.external_consumers]]\n"
        "# name is intentionally missing\n"
        'kind = "tableau"\n'
        'consumes = ["ia_sales.fct_orders"]\n'
        "\n"
        "[[sqlcg.external_consumers]]\n"
        'name = "Valid Consumer"\n'
        'kind = "feed"\n'
        'consumes = ["ia_sales.fct_orders"]\n'
    )
    (tmp_path_fixture / ".sqlcg.toml").write_text(toml)

    specs = get_external_consumers(tmp_path_fixture)

    names = [s.name for s in specs]
    assert "Valid Consumer" in names, f"Valid Consumer must be present; got {names}"
    # The nameless row must not appear
    assert len(specs) == 1, (
        f"Expected exactly 1 spec (nameless row skipped); got {len(specs)}: {names}"
    )


def test_T35_CFG_2c_row_empty_consumes_skipped(tmp_path_fixture):
    """T35-CFG-2c: a row with an empty `consumes` list is silently skipped."""
    if not _SYMBOLS_AVAILABLE:
        pytest.skip("get_external_consumers not yet implemented (#35)")

    toml = (
        "[[sqlcg.external_consumers]]\n"
        'name = "Empty Consumer"\n'
        'kind = "feed"\n'
        "consumes = []\n"
        "\n"
        "[[sqlcg.external_consumers]]\n"
        'name = "Valid Consumer"\n'
        'kind = "feed"\n'
        'consumes = ["ia_sales.fct_orders"]\n'
    )
    (tmp_path_fixture / ".sqlcg.toml").write_text(toml)

    specs = get_external_consumers(tmp_path_fixture)

    names = [s.name for s in specs]
    assert "Valid Consumer" in names, f"Valid Consumer must be present; got {names}"
    assert "Empty Consumer" not in names, (
        f"Empty Consumer must be skipped (empty consumes); got {names}"
    )
    assert len(specs) == 1, (
        f"Expected exactly 1 spec (empty-consumes row skipped); got {len(specs)}: {names}"
    )


# ---------------------------------------------------------------------------
# Wiring: ExternalConsumerSpec is a Pydantic BaseModel with expected fields
# ---------------------------------------------------------------------------


def test_T35_CFG_spec_has_required_fields(tmp_path_fixture):
    """ExternalConsumerSpec must have name, consumer_type, and consumes fields."""
    if not _SYMBOLS_AVAILABLE:
        pytest.skip("ExternalConsumerSpec not yet implemented (#35)")

    toml = (
        "[[sqlcg.external_consumers]]\n"
        'name = "Test"\n'
        'kind = "feed"\n'
        'consumes = ["ia_sales.fct_orders"]\n'
    )
    (tmp_path_fixture / ".sqlcg.toml").write_text(toml)

    specs = get_external_consumers(tmp_path_fixture)
    assert len(specs) == 1
    spec = specs[0]
    assert hasattr(spec, "name"), "ExternalConsumerSpec must have .name"
    assert hasattr(spec, "consumer_type"), "ExternalConsumerSpec must have .consumer_type"
    assert hasattr(spec, "consumes"), "ExternalConsumerSpec must have .consumes"
    assert isinstance(spec.consumes, list), f".consumes must be a list; got {type(spec.consumes)}"
    assert spec.name == "Test"
    assert spec.consumer_type == "feed"
    assert spec.consumes == ["ia_sales.fct_orders"]
