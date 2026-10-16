from __future__ import annotations

TOPIC_EXPANSION: dict[str, dict[str, list[str]]] = {
    "量化策略": {
        "zh": [
            "量化策略", "量化投资", "量化交易", "量化选股", "量化模型",
            "主动量化", "量化对冲", "回测", "系统化交易", "程序化交易",
        ],
        "en": [
            "quantitative strategy", "quant trading", "systematic trading",
            "backtest", "quant investing",
        ],
    },
    "因子模型": {
        "zh": [
            "因子模型", "多因子", "因子选股", "因子投资", "因子策略",
            "风格因子", "alpha因子", "Smart Beta", "因子收益", "因子暴露",
            "Barra", "Fama-French",
        ],
        "en": [
            "factor model", "multi-factor", "factor investing", "smart beta",
            "factor exposure", "Fama-French", "Barra",
        ],
    },
    "高频交易": {
        "zh": [
            "高频交易", "高频策略", "做市商", "微观结构", "市场微观结构",
            "限价单", "订单簿", "低延迟", "tick数据",
        ],
        "en": [
            "high frequency trading", "HFT", "market making",
            "market microstructure", "order book", "limit order book",
            "low latency", "tick data",
        ],
    },
    "风险模型": {
        "zh": [
            "风险模型", "风险管理", "风险控制", "风险度量", "VaR",
            "风险平价", "风险预算", "压力测试", "尾部风险", "回撤",
        ],
        "en": [
            "risk model", "risk management", "value at risk", "VaR",
            "risk parity", "stress testing", "drawdown", "tail risk",
        ],
    },
    "AI/机器学习": {
        "zh": [
            "机器学习", "深度学习", "神经网络", "强化学习",
            "自然语言处理", "AI选股", "AI量化", "LSTM", "Transformer",
            "随机森林", "XGBoost", "梯度提升",
        ],
        "en": [
            "machine learning", "deep learning", "neural network",
            "reinforcement learning", "NLP", "LSTM", "transformer",
            "random forest", "XGBoost", "gradient boosting",
        ],
    },
    "执行算法": {
        "zh": [
            "执行算法", "算法交易", "程序化交易", "交易成本",
            "TWAP", "VWAP", "冲击成本", "滑点", "最优执行",
        ],
        "en": [
            "execution algorithm", "algorithmic trading", "TWAP", "VWAP",
            "transaction cost", "market impact", "optimal execution",
            "slippage",
        ],
    },
    "组合优化": {
        "zh": [
            "组合优化", "资产配置", "均值方差", "Black-Litterman",
            "组合管理", "再平衡", "风险预算", "有效前沿",
            "马科维茨", "最优组合",
        ],
        "en": [
            "portfolio optimization", "asset allocation", "mean-variance",
            "Black-Litterman", "Markowitz", "rebalancing",
            "efficient frontier",
        ],
    },
    "统计套利": {
        "zh": [
            "统计套利", "配对交易", "套利策略", "市场中性",
            "协整", "均值回复", "价差交易",
        ],
        "en": [
            "statistical arbitrage", "pairs trading", "market neutral",
            "cointegration", "mean reversion", "spread trading",
        ],
    },
    "波动率": {
        "zh": [
            "波动率", "隐含波动率", "波动率曲面", "期权定价",
            "GARCH", "波动率模型", "期权策略", "Greeks",
            "随机波动率", "局部波动率",
        ],
        "en": [
            "volatility", "implied volatility", "volatility surface",
            "option pricing", "GARCH", "stochastic volatility",
            "local volatility", "Greeks",
        ],
    },
    "另类数据": {
        "zh": [
            "另类数据", "替代数据", "卫星数据", "舆情数据",
            "情绪分析", "文本分析", "社交媒体", "新闻情绪",
        ],
        "en": [
            "alternative data", "satellite data", "sentiment analysis",
            "text mining", "NLP finance", "news sentiment",
        ],
    },
    "market microstructure": {
        "zh": [
            "市场微观结构", "限价单", "订单簿", "做市商",
            "价格发现", "流动性", "买卖价差",
        ],
        "en": [
            "market microstructure", "limit order book", "order book",
            "market making", "price discovery", "liquidity",
            "bid-ask spread",
        ],
    },
    "factor investing": {
        "zh": [
            "因子投资", "因子模型", "多因子", "Smart Beta", "风格因子",
        ],
        "en": [
            "factor investing", "factor model", "multi-factor",
            "smart beta", "factor exposure", "Fama-French",
        ],
    },
    "portfolio optimization": {
        "zh": [
            "组合优化", "资产配置", "均值方差", "组合管理",
        ],
        "en": [
            "portfolio optimization", "asset allocation", "mean-variance",
            "efficient frontier", "rebalancing",
        ],
    },
}


def expand_topics(
    topics: list[str],
    keywords: list[str] | None = None,
    lang: str = "all",
) -> list[str]:
    """Expand user-selected topics into a broader set of search terms.

    Args:
        topics: Raw topic strings from the frontend (e.g. ["量化策略", "因子模型"])
        keywords: Additional user keywords to include as-is
        lang: "zh" for Chinese only, "en" for English only, "all" for both
    """
    expanded: list[str] = []
    seen: set[str] = set()

    for topic in topics:
        mapping = TOPIC_EXPANSION.get(topic)
        if mapping:
            if lang in ("zh", "all"):
                for term in mapping["zh"]:
                    low = term.lower()
                    if low not in seen:
                        seen.add(low)
                        expanded.append(term)
            if lang in ("en", "all"):
                for term in mapping["en"]:
                    low = term.lower()
                    if low not in seen:
                        seen.add(low)
                        expanded.append(term)
        else:
            low = topic.lower()
            if low not in seen:
                seen.add(low)
                expanded.append(topic)

    if keywords:
        for kw in keywords:
            low = kw.lower()
            if low not in seen:
                seen.add(low)
                expanded.append(kw)

    return expanded
