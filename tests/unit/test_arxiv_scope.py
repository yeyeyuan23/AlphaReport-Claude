"""Configured category scope preserves finance and filters transferable methods."""

import pytest

from reportagent.sources.arxiv_scope import ArxivSearchScope


SCOPE = {
    "full_categories": ["q-fin"],
    "topic_groups": [
        {
            "name": "ml",
            "categories": ["cs.LG", "stat.ML"],
            "keywords": ["time series", "online learning"],
        },
        {
            "name": "optimization",
            "categories": ["math.OC"],
            "keywords": ["portfolio optimization", "optimal execution"],
        },
    ],
}


@pytest.mark.parametrize(
    "categories,title,abstract,accepted",
    [
        (["q-fin"], "Stochastic calculus", "A financial theorem", True),
        (
            ["cs.LG"],
            "TIME-SERIES prediction",
            "A method without financial keywords",
            True,
        ),
        (["stat.ML"], "Adaptive estimators", "We study online learning", True),
        (["math.OC"], "Allocation", "Portfolio optimization with costs", True),
        (["cs.LG"], "Medical images", "An offline learning method", False),
        (["cs.LG"], "Outline learning", "The methodology of a classifier", False),
        (["math.OC"], "Online learning", "A general learning method", False),
        (["cs.CL"], "Time series", "An unconfigured category", False),
    ],
)
def test_scope_filters_title_and_abstract_in_the_matching_category(
    categories, title, abstract, accepted
):
    decision = ArxivSearchScope.from_config(SCOPE).match(categories, title, abstract)
    assert bool(decision) is accepted


def test_cross_listing_to_qfin_retains_paper_without_topic_keywords():
    scope = ArxivSearchScope.from_config(SCOPE)
    assert scope.match(["cs.LG", "q-fin"], "General method", "No keyword")
    assert scope.categories == ("q-fin", "cs.LG", "stat.ML", "math.OC")


@pytest.mark.parametrize(
    "config",
    [
        {"full_categories": "q-fin"},
        {"full_categories": ["../q-fin"]},
        {"topic_groups": [{"name": "ml", "categories": ["cs.LG"], "keywords": []}]},
        {"topic_groups": [{"name": "ml", "categories": ["cs.LG"], "keywords": [""]}]},
    ],
)
def test_invalid_scope_is_rejected_instead_of_silently_expanding_or_losing_coverage(
    config,
):
    with pytest.raises(ValueError):
        ArxivSearchScope.from_config(config)


def test_publication_query_uses_same_configured_categories_and_title_abstract_terms():
    query = ArxivSearchScope.from_config(SCOPE).query()
    assert "cat:q-fin.*" in query
    assert "cat:cs.LG" in query and "cat:stat.ML" in query
    assert 'ti:"time series"' in query and 'abs:"time series"' in query
    assert "cat:math.OC" in query and 'abs:"optimal execution"' in query
    assert "cs.AI" not in query and "all:" not in query


def test_topic_phrase_cannot_bridge_title_and_abstract():
    scope = ArxivSearchScope.from_config(SCOPE)
    assert not scope.match(["cs.LG"], "An online", "Learning system for images")
