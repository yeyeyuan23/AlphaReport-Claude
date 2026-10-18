from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Precise research phrases allow Chinese reports to reach semantic classification.
# Generic terms such as 模型、资产配置 and 量化 are intentionally insufficient.
CHINESE_QUANT_PHRASES = (
    "多因子",
    "因子模型",
    "因子选股",
    "因子挖掘",
    "因子投资",
    "量化投资",
    "量化策略",
    "量化选股",
    "量化交易",
    "宏观量化",
    "金融工程",
    "金工",
    "组合优化",
    "风险平价",
    "风险预算",
    "统计套利",
    "算法交易",
    "高频交易",
    "市场微观结构",
    "波动率模型",
    "期权定价",
    "指数增强",
    "市场中性",
)

# Positive indicators that a paper IS quantitative finance.
# Keywords are stems — matched case-insensitively against title + abstract.
QUANT_SIGNALS: dict[str, list[str]] = {
    "math_stat": [
        "regress",
        "optimiz",
        "stochastic",
        "algorithm",
        "model",
        "estimat",
        "infer",
        "predict",
        "forecast",
        "probabil",
        "distribut",
        "covariance",
        "correlat",
        "time series",
        "panel data",
        "cross-section",
        "bootstrap",
        "monte carlo",
        "bayesian",
        "maximum likelihood",
        "regulariz",
        "gradient",
        "converg",
        "loss function",
        "objective function",
        "sharpe",
        "sortino",
        "information ratio",
        "maximum drawdown",
        "coefficient",
        "variable",
        "parameter",
        "matrix",
        "vector",
        "likelihood",
        "momentum",
        "linear",
        "nonlinear",
        "quadratic",
        "norm",
        "constraint",
        "residual",
        "error term",
        "r-squared",
        "neural",
        "network",
        "layer",
        "activation",
        "backpropagat",
        "loss",
        "accuracy",
        "precision",
        "recall",
        "auc",
        "markov",
        "bellman",
        "q-learning",
        "policy gradient",
        "state space",
        "kalman",
        "particle filter",
        "hidden markov",
        "regime switch",
    ],
    "quant_method": [
        "factor model",
        "risk model",
        "machine learning",
        "deep learning",
        "neural network",
        "lstm",
        "transformer",
        "attention",
        "random forest",
        "xgboost",
        "gradient boost",
        "reinforcement learning",
        "garch",
        "arch",
        "stochastic volatility",
        "cointegrat",
        "principal component",
        "pca",
        "autoencoder",
        "gan",
        "natural language process",
        "nlp",
        "sentiment",
        "support vector",
        "k-means",
        "cluster",
        "classif",
        "tree",
        "ensemble",
        "bagging",
        "boosting",
        "graph neural",
        "diffusion model",
        "generative",
        "regime switch",
        "kalman filter",
        "markov model",
        "actor-critic",
        "ppo",
        "dqn",
        "mdp",
        "bert",
        "finbert",
        "roberta",
        "gpt",
        "llm",
        "large language model",
        "foundation model",
    ],
    "quant_finance": [
        "alpha",
        "beta",
        "factor",
        "signal",
        "portfolio",
        "asset pric",
        "return predict",
        "volatil",
        "risk premium",
        "backtest",
        "strategy",
        "trading",
        "execution",
        "market impact",
        "order book",
        "limit order",
        "market mak",
        "arbitrage",
        "hedg",
        "derivative",
        "option pric",
        "futures",
        "sharpe",
        "sortino",
        "drawdown",
        "var",
        "cvar",
        "expected return",
        "excess return",
        "risk-free",
        "stock select",
        "stock predict",
        "equity",
        "quant",
        "quantitative",
        "financial engineering",
        "a-share",
        "csi",
        "sse",
        "szse",
        "china market",
        "factor timing",
        "market timing",
        "economic indicator",
        "long-short",
        "long only",
        "top-minus-bottom",
        "ic",
        "rank ic",
        "icir",
        "information coeffic",
        "earnings",
        "transcript",
        "filing",
        "10-k",
        "10-q",
        "analyst",
        "news",
        "macroeconomic",
    ],
    "data_empirical": [
        "empiric",
        "dataset",
        "sample period",
        "out-of-sample",
        "backtest",
        "transaction cost",
        "turnover",
        "cross-section",
        "portfolio construct",
        "train set",
        "test set",
        "validat set",
        "implement",
        "benchmark",
        "baseline",
        "simulat",
        "annualized",
        "cumulative",
        "sharpe",
        "information ratio",
        "maximum drawdown",
        "daily return",
        "monthly return",
        "stock universe",
    ],
}


def _stem_matches(stem: str, text: str) -> bool:
    """Check if stem appears in text with word-boundary awareness.

    Stems with length <= 4 use regex \\b boundaries to avoid matching
    inside longer words (e.g., 'arch' matching 'research', 'ic' matching 'practice').
    Longer stems use substring matching which correctly finds word variants
    (e.g., 'predict' matches 'prediction', 'predicting').
    """
    if " " in stem:
        return stem in text

    if len(stem) <= 4:
        import re

        return bool(re.search(rf"\b{re.escape(stem)}", text))

    return stem in text


def compute_quant_score(
    title: str, abstract: str = ""
) -> tuple[float, dict[str, float]]:
    """Return (total_score, category_scores) for a paper's quant relevance.

    Uses stem-based matching: keyword stems like 'predict' match 'prediction',
    'predictive', 'predicting' etc.
    """
    text = f"{title} {abstract}".lower()
    cat_scores: dict[str, float] = {}

    for cat, stems in QUANT_SIGNALS.items():
        hits = 0
        for stem in stems:
            if _stem_matches(stem, text):
                hits += 1
        cat_scores[cat] = hits / len(stems) if stems else 0.0

    chinese_hits = sum(phrase in text for phrase in CHINESE_QUANT_PHRASES)
    # Keep existing English calibration unchanged. One precise Chinese phrase
    # contributes a small finance signal (weighted 0.0175), sufficient for recall
    # at the default coarse cutoff, never a research-quality recommendation.
    cat_scores["quant_finance"] = max(
        cat_scores["quant_finance"], min(1.0, 0.05 * chinese_hits)
    )

    # Weighted: quant_finance and math_stat are heavier signals
    weights = {
        "math_stat": 0.25,
        "quant_method": 0.15,
        "quant_finance": 0.35,
        "data_empirical": 0.25,
    }
    total = sum(cat_scores.get(c, 0) * weights.get(c, 0.1) for c in QUANT_SIGNALS)

    return round(total, 4), {k: round(v, 4) for k, v in cat_scores.items()}


def is_quant_finance(title: str, abstract: str = "", threshold: float = 0.015) -> bool:
    """Quick keyword-based quant relevance check.

    threshold=0.015 is calibrated on 150+ word real arXiv abstracts.
    With very short abstracts (<50 words), borderline papers may need
    the LLM fallback via is_quant_finance_llm().
    """
    score, _ = compute_quant_score(title, abstract)
    return score >= threshold


async def is_quant_finance_llm(
    title: str,
    abstract: str,
    threshold: float = 0.015,
    source_category: str = "",
) -> bool:
    """Two-tier quant relevance check: keyword first, LLM for borderline + cross-cat.

    Cross-category papers (cs.AI, cs.LG, stat.ML, cs.CL, math.OC) ALWAYS go through
    LLM check because their titles/abstracts may contain math/ML keywords without
    being quant finance.
    """
    kw_score, cat_scores = compute_quant_score(title, abstract)

    # Cross-category papers with low finance signal: always LLM-check
    CROSS_CATS = {"cs.AI", "cs.LG", "stat.ML", "cs.CL", "math.OC"}
    is_cross_cat = source_category in CROSS_CATS

    # Clear pass (strong keyword signals from q-fin categories)
    if kw_score >= threshold * 3 and not is_cross_cat:
        return True

    # Clear reject: near-zero signals across all categories
    if kw_score < threshold * 0.3:
        logger.debug("Quant filter REJECT (score=%.4f): %s", kw_score, title[:60])
        return False

    # Cross-category or gray zone: ask LLM
    if is_cross_cat:
        logger.debug(
            "Quant filter: cross-cat LLM check for %s (cat=%s)",
            title[:60],
            source_category,
        )
    else:
        logger.debug("Quant filter gray zone (score=%.4f): %s", kw_score, title[:60])
    try:
        from reportagent.llm.client import LLMClient

        prompt = (
            "判断以下论文是否属于**量化金融**领域。\n\n"
            "✅ 属于量化金融（回答 is_quant: true）：\n"
            "- 使用数学模型/统计/机器学习方法研究金融问题（定价、交易、风控、组合优化、因子挖掘等）\n"
            "- 金融市场微观结构、订单流分析、市场影响建模\n"
            "- 金融时间序列预测（波动率、收益、相关性）\n"
            "- 另类数据在投资中的应用（NLP财报、卫星图像用于经济预测等）\n"
            "- 衍生品定价、随机波动率模型\n"
            "- 量化行为金融（用定量方法研究投资者行为对市场的影响）\n\n"
            "❌ 不属于量化金融（回答 is_quant: false）：\n"
            "- 纯计算机视觉/图像处理（即使标题含'robust'/'uncertainty'）\n"
            "- 纯NLP/LLM技术（KV cache、推理加速、模型蒸馏等）不涉及金融应用\n"
            "- 纯数学/物理/量子计算（PDE求解、算子学习、量子算法等不涉及金融场景）\n"
            "- 机器人/自动驾驶/具身智能\n"
            "- 生物信息学/医学影像\n"
            "- 纯图神经网络/纯强化学习理论不涉及金融\n"
            "- 金融监管政策、法律合规（纯定性研究）\n"
            "- 纯宏观经济评论（不用定量方法）\n\n"
            f"标题：{title}\n\n"
            f"摘要：{abstract}\n\n"
            '返回 JSON: {{"is_quant": true/false, "reason": "一句话理由"}}'
        )
        client = LLMClient()
        resp = await client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        result = resp.get("is_quant", False)
        logger.debug("Quant filter LLM: %s -> %s", title[:60], result)
        return result
    except Exception as e:
        logger.warning("Quant LLM check failed, falling back to keyword: %s", e)
        return kw_score >= threshold
