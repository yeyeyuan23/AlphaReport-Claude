"""Opt-in semantic regression for claim-specific validation, using the real model.

Run with ALPHAREPORT_RUN_MODEL_TESTS=1. Set ALPHAREPORT_CLAIM_TEST_OUTPUT
to retain requests and results in a chosen acceptance directory. No DB writes
or messages are sent. Synthetic documents share a data table and vary their claims.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from reportagent.llm.client import LLMClient
from reportagent.services.report_quality import evaluate_paper


pytestmark = pytest.mark.skipif(
    os.environ.get("ALPHAREPORT_RUN_MODEL_TESTS") != "1",
    reason="Real model calls require explicit opt-in",
)


DATA = """
本期资料观察区间为2026年9月28日收盘至2026年10月9日收盘。基金份额表单位均为亿份，
单位净值为元/份，行业持股数量另以万股列示。份额不是价格，持有数量不是持仓市值。
原始观察值如下：

基金代码 | 9月28日份额 | 10月9日份额 | 观察期份额净变动
甲 | 100 | 104 | +4
乙 | 80 | 78 | -2
丙 | 50 | 53 | +3
丁 | 70 | 69 | -1
合计 | 300 | 304 | +4

另列各基金9月28日、10月9日的单位净值：甲1.10、1.12；乙0.95、0.93；丙1.20、1.22；
丁1.00、1.01。份额净变动直接由两端份额相减得到，合计逐行相加。若要估算资金流量，
需要另行指定申赎发生时的价格，因此此表的份额变化不转换为资金净流入金额，也不将
两端资产净值之差解释为主动申赎金额。原始份额观察值与净值观察值分别列示，避免混用。

为便于复核，表中四项份额变动相加为4-2+3-1=4，期初份额相加为300，期末为304，
合计增幅为4/300=1.3333%。甲、丙份额增长，乙、丁份额下降。期末份额比例分别为
104/304、78/304、53/304、69/304，其和为100%。所有汇总先对原值运算，再保留四位
小数，显示值的尾数误差不回填到原始表格。区间内节假日不补造交易日记录。

本期还观察了两端的行业持股数量。电子、医药、机械三个行业的期初持股数量分别为
1000、800、600万股，期末为1050、780、620万股，变动为+50、-20、+20万股，合计
持股数量从2400万股变为2450万股。表内标的在两个时点均存在，期间没有拆股、合股、
股票分红和样本纳入剔除，因而这两个时点的持股数量可按相同口径比较。行业分类固定
为期初分类，期间没有把分类调整造成的转入转出记作买卖。只使用表内这三个行业的
固定样本，报告不把它们的合计数写成整个市场的总量。

数据整理采用双人交叉录入，每一行保留两端观察日与原始数值，并逐项核对合计。
这些程序只降低抄录与计算错误，不证明样本代表整个市场。所列净值只用于区分价格
变化与份额变化，不用于估计基金未来收益。行业持股数量的变动仅描述固定样本在
观察区间内的变化，不等同于市场总买卖金额。正文比较、表头日期与上述观察区间一致。

本期可直接得到的观察为：四只基金合计份额增长4亿份，甲和丙增长、乙和丁下降；
固定行业样本内电子和机械持股数量增加，医药下降，合计增加50万股。以上均可由给定
原始表格重新计算。统计覆盖范围、单位及两端时间保持不变。
""".strip()


CASES = {
    "descriptive": (
        "本期基金份额与行业持股数量观察",
        "结论：上述表格反映了本期固定样本的份额与持股数量变动。",
        {"supported"},
    ),
    "predictive": (
        "本期观察与下期收益预测",
        "结论：由本期甲和丙份额增加可预测下个月甲和丙必然跑赢乙和丁，"
        "该预测对所有未来月份均有效。预测效果的依据只有上述这一期表格。",
        {"limited", "unclear"},
    ),
    "causal": (
        "基金份额变化对价格的因果作用",
        "结论：本期表格证明份额增长是净值上涨的唯一原因，并证明任何市场条件下"
        "增加申购都会直接提高净值。因果结论的依据只有上述两端同期变化。",
        {"limited", "unclear"},
    ),
    "mixed_strategy": (
        "本期资金观察与可交易策略",
        "结论一：本期甲和丙份额增长，乙和丁份额下降。结论二：未来每月买入当月"
        "份额增长最大的两只基金，就能在所有市场阶段获得稳定的税费后超额收益，"
        "可立即实盘交易。策略效果的依据只有上述这一期表格，未给出交易规则细节、"
        "信号可用时点、手续费、滑点处理或历史策略回测结果。",
        {"limited", "unclear"},
    ),
    "unvalidated_estimate": (
        "本期基金股票仓位测算",
        "主要结论：由上述份额与净值表测算，四只基金当前平均股票仓位为87%，"
        "比上期增加4.2个百分点，因此股票仓位本期明显上升。这些都是测算值，非披露值，"
        "存在测算误差。除上述表格外，未提供仓位测算的公式、输入资产收益、估计约束、"
        "可核对的参考仓位、误差范围或敏感性分析。",
        {"limited", "unclear"},
    ),
    "numerical_method": (
        "固定数据的求和算法比较",
        "主要结论：对文中四行基金份额变动求和，算法甲从左到右计算，得到"
        "4+(-2)+3+(-1)=4；算法乙分组计算，得到(4+(-2))+(3+(-1))=2+2=4。"
        "两个算法在这组整数上结果完全一致，都是三次加法；使用精确整数运算，无舍入。"
        "结论仅限此表的运算结果与加法次数，不声称算法在浮点数上误差相同、不声称"
        "运行时间更快，也不声称能预测基金收益。两种运算步骤已逐项列出可供复核。",
        {"supported"},
    ),
    "inconsistent_dates": (
        "本期基金份额变动统计",
        "表1脚注：本表期初为2026年6月30日，期末为2026年9月30日。"
        "主要结论：表1显示四只基金从2026年9月28日至10月9日的合计份额增长4亿份，"
        "该变动完全代表上述观察期内的变化，表格与正文日期一致。",
        {"supported", "limited", "unclear"},
    ),
}


def test_real_model_selects_validation_by_claim():
    output = Path(
        os.environ.get(
            "ALPHAREPORT_CLAIM_TEST_OUTPUT",
            "data/acceptance/claim-validation-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        )
    )
    output.mkdir(parents=True, exist_ok=True)

    async def one(name, title, conclusion):
        client = LLMClient(thinking="disabled")
        record = {"case": name, "title": title, "model": client.model}
        original = client.chat_json

        async def audited(messages, **kwargs):
            record["messages"] = messages
            value = await original(messages, **kwargs)
            record["response"] = value
            return value

        client.chat_json = audited
        try:
            text = DATA + "\n\n" + conclusion
            assert len(text) >= 1000
            record["evaluation"] = await evaluate_paper(
                {
                    "title": title,
                    "abstract": conclusion,
                    "full_text": text,
                    "has_full_text": True,
                },
                client,
            )
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            record["usage"] = client.calls
            await client._client.close()
            (output / f"{name}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return record

    async def run():
        gate = asyncio.Semaphore(4)

        async def bounded(name, title, claim):
            async with gate:
                return await one(name, title, claim)

        return await asyncio.gather(
            *(bounded(name, title, claim) for name, (title, claim, _) in CASES.items())
        )

    records = asyncio.run(run())
    failures = []
    for record in records:
        name = record["case"]
        if "error" in record:
            failures.append(f"{name}: {record['error']}")
            continue
        result = record["evaluation"]
        check = next(
            c for c in result["quality_checks"] if c["dimension"] == "validation"
        )
        if check["status"] not in CASES[name][2]:
            failures.append(f"{name}: {check}")
        if name == "inconsistent_dates":
            # Arithmetic can be supported while date uncertainty is recorded in
            # scope/reproducibility; do not prescribe which dimension must fall.
            issues = [
                c
                for c in result["quality_checks"]
                if c["status"] in {"limited", "unclear"}
            ]
            if result["confidence"] == "high" or not any(
                "脚注" in c["basis"] for c in issues
            ):
                failures.append(
                    f"{name}: date conflict must limit the assessment: {result}"
                )
    assert not failures, "\n".join(failures) + f"\nAudit: {output}"
