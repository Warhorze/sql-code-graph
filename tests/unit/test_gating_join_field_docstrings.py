"""Unit tests: gating_join_tables field docstrings carry both required caveats.

PR 3 (v1.24.0) adds ``gating_join_tables`` to ``ChangeScopeResult`` and
``DiffImpactResult``. The plan mandates that the field description carries BOTH:
  1. "CTE-wrapped pure-gating reads are NOT detected" (known gap)
  2. "depends on join type" (upper bound)

These are string-assertion tests on the Pydantic model field metadata — they
pin the honest-semantics contract so neither caveat can be silently removed.

Guards plan/sprints/unfilled_table_impact.md PR 3 Step 3.1 acceptance.
"""

from __future__ import annotations

import pytest

from sqlcg.server.models import ChangeScopeResult, DiffImpactResult


@pytest.mark.parametrize("model_cls", [ChangeScopeResult, DiffImpactResult])
def test_gating_join_field_description_has_cte_gap_caveat(model_cls):
    """gating_join_tables description contains the CTE-pure-gating known-gap caveat.

    The string "CTE-wrapped pure-gating reads are NOT detected" must appear in the
    Pydantic field description for both ChangeScopeResult and DiffImpactResult.

    Guards plan/sprints/unfilled_table_impact.md PR 3 Step 3.1 acceptance.
    """
    field = model_cls.model_fields["gating_join_tables"]
    desc = field.description or ""
    assert "CTE-wrapped pure-gating reads are NOT detected" in desc, (
        f"{model_cls.__name__}.gating_join_tables description missing CTE-gap caveat. Got: {desc!r}"
    )


@pytest.mark.parametrize("model_cls", [ChangeScopeResult, DiffImpactResult])
def test_gating_join_field_description_has_join_type_caveat(model_cls):
    """gating_join_tables description contains the join-type upper-bound caveat.

    The string "depends on join type" must appear in the Pydantic field description
    for both ChangeScopeResult and DiffImpactResult.

    Guards plan/sprints/unfilled_table_impact.md PR 3 Step 3.1 acceptance.
    """
    field = model_cls.model_fields["gating_join_tables"]
    desc = field.description or ""
    assert "depends on join type" in desc, (
        f"{model_cls.__name__}.gating_join_tables description missing join-type caveat. "
        f"Got: {desc!r}"
    )


@pytest.mark.parametrize("model_cls", [ChangeScopeResult, DiffImpactResult])
def test_gating_join_field_default_is_empty_list(model_cls):
    """gating_join_tables defaults to an empty list (backward-additive).

    Existing callers that do not set gating_join_tables must receive [].
    Guards plan/sprints/unfilled_table_impact.md PR 3 Output shape.
    """
    field = model_cls.model_fields["gating_join_tables"]
    # default_factory=list → field.default_factory is list
    assert field.default_factory is list, (
        f"{model_cls.__name__}.gating_join_tables must have default_factory=list, "
        f"got default_factory={field.default_factory!r}"
    )
