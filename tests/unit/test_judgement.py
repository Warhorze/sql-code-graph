"""Unit tests for the Judgement discriminator model (trust layer foundation).

Step 1.1 acceptance criteria:
  - Fact carrying confidence/reason raises ValidationError.
  - Heuristic missing confidence/reason raises ValidationError.
  - Valid heuristic (with confidence and reason) constructs and has correct fields.
"""

import pytest
from pydantic import ValidationError

from sqlcg.server.models import Judgement


def test_fact_with_confidence_raises():
    """A fact Judgement must not carry confidence — validator rejects it."""
    with pytest.raises(ValidationError):
        Judgement(
            assertion_type="fact",
            label="exact_count",
            confidence=0.9,
        )


def test_fact_with_reason_raises():
    """A fact Judgement must not carry reason — validator rejects it."""
    with pytest.raises(ValidationError):
        Judgement(
            assertion_type="fact",
            label="exact_count",
            reason="some reason",
        )


def test_heuristic_without_reason_raises():
    """A heuristic Judgement without reason raises ValidationError."""
    with pytest.raises(ValidationError):
        Judgement(
            assertion_type="heuristic",
            label="high",
            confidence=0.6,
            # reason omitted
        )


def test_heuristic_without_confidence_raises():
    """A heuristic Judgement without confidence raises ValidationError."""
    with pytest.raises(ValidationError):
        Judgement(
            assertion_type="heuristic",
            label="high",
            reason="57 downstream dependents >= threshold 20",
            # confidence omitted
        )


def test_valid_heuristic_constructs():
    """A heuristic with both confidence and reason constructs successfully."""
    j = Judgement(
        assertion_type="heuristic",
        label="high",
        confidence=0.7,
        reason="57 downstream dependents >= threshold 20",
    )
    assert j.assertion_type == "heuristic"
    assert j.label == "high"
    assert j.confidence == 0.7
    assert j.reason == "57 downstream dependents >= threshold 20"


def test_valid_fact_constructs():
    """A fact with no confidence/reason constructs successfully."""
    j = Judgement(assertion_type="fact", label="42")
    assert j.assertion_type == "fact"
    assert j.label == "42"
    assert j.confidence is None
    assert j.reason is None
