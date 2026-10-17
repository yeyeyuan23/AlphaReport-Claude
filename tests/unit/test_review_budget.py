"""Review budgets are optional, explicit positive counts shared by both reports."""

import pytest

from reportagent.services import report_quality as quality


@pytest.mark.parametrize(
    "override,settings,expected",
    [
        (None, {}, None),
        (None, {"max_reviews": None}, None),
        (None, {"max_reviews": 20}, 20),
        (30, {"max_reviews": 2}, 30),
    ],
)
def test_optional_budget_precedence(override, settings, expected):
    assert quality.resolve_review_budget(override, settings) == expected


@pytest.mark.parametrize("invalid", [0, -1, True, 2.5, "12", "unlimited"])
@pytest.mark.parametrize("source", ["cli", "config"])
def test_invalid_budget_cannot_silently_remove_or_shrink_a_limit(invalid, source):
    override, settings = (
        (invalid, {}) if source == "cli" else (None, {"max_reviews": invalid})
    )
    with pytest.raises(ValueError, match="max_reviews"):
        quality.resolve_review_budget(override, settings)
