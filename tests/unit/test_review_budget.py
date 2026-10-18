"""Collection review budgets are optional, explicit positive counts."""

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


@pytest.mark.parametrize(
    "source,invalid",
    [("cli", 0), ("cli", True), ("cli", 2.5), ("cli", "12"), ("config", True)],
)
def test_invalid_budget_cannot_silently_remove_or_shrink_a_limit(invalid, source):
    override, settings = (
        (invalid, {}) if source == "cli" else (None, {"max_reviews": invalid})
    )
    with pytest.raises(ValueError, match="max_reviews"):
        quality.resolve_review_budget(override, settings)
