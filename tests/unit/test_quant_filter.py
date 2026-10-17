"""Chinese research must reach classification without admitting generic news."""

import pytest

from reportagent.classifiers.quant_filter import is_quant_finance


@pytest.mark.parametrize(
    "title",
    [
        "渤海证券-多因子选股系列专题报告之一-多因子模型初探",
        "【方正金工】波动率的波动率与投资者模糊性厌恶——多因子选股系列研究之五",
        "华安证券-基于端到端神经网络的风险预算与组合优化",
        "宏观量化经济指数周报：9月PPI同比增速或录得年内新高",
    ],
)
def test_chinese_quant_research_reaches_semantic_classification(title):
    assert is_quant_finance(title, "")


@pytest.mark.parametrize(
    "title",
    [
        "美联储量化宽松政策展望",
        "轻量化汽车材料研究",
        "资产配置双周报：中债与海外债市为何表现分化？",
        "神经网络图像分割模型",
    ],
)
def test_generic_chinese_commentary_is_not_quant_research(title):
    assert not is_quant_finance(title, "")


def test_english_score_remains_calibrated():
    assert is_quant_finance(
        "Portfolio optimization with transaction costs",
        "A quantitative finance model predicts portfolio returns and risk.",
    )
