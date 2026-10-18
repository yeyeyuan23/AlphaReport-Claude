import asyncio

import pytest

from tests.acceptance.source_calibration import (
    blind_cases,
    evaluate_cases,
    validate_labels,
)
from tests.support.report_fixtures import Model, paper, response


def test_blind_cases_exclude_source_metadata_and_saved_model_judgments():
    cases, key = blind_cases([paper(source="bigquant", evaluation_json="old label")])
    assert set(cases[0]["paper"]) == {"title", "abstract", "full_text", "has_full_text"}
    assert key["B001"]["source"] == "bigquant"
    assert cases[0]["paper"]["full_text"] == paper()["full_text"]


def test_human_labels_are_required_before_model_calibration():
    cases, _ = blind_cases([paper()])
    model = Model(response())
    with pytest.raises(ValueError):
        asyncio.run(evaluate_cases(cases, {}, model))
    assert not model.requests


def test_model_calibration_compares_with_prior_human_label_without_source_input():
    cases, _ = blind_cases([paper(source="eastmoney")])
    model = Model(response())
    labels = {"B001": {"quality": "sound", "basis": "验证存在明确边界，方法有依据。"}}
    result = asyncio.run(evaluate_cases(cases, labels, model))
    assert not result["B001"]["quality_agrees"]
    assert result["B001"]["evaluation"]["quality"] == "strong"
    assert "eastmoney" not in model.requests[0][1]["content"]


def test_human_label_cannot_be_a_bare_model_grade():
    cases, _ = blind_cases([paper()])
    with pytest.raises(ValueError):
        validate_labels({"B001": {"quality": "strong"}}, cases)
