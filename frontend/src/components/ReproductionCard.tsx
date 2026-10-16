import { useEffect, useRef, useState } from "react";
import { getReproductionCard, prepareReproductionCard } from "@/services/api";
import type { ReproductionCard as Preparation } from "@/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";

const labels: Record<string, string> = {
  target: "研究／预测目标", available_information: "推断时可用信息", training_objective: "训练目标",
  data: "数据与样本", split: "训练、验证与测试划分", baseline: "比较基准",
  metrics: "评价指标", assumptions: "适用假设与约束",
};
const statusLabels = { documented: "原文有依据", needs_verification: "待核查", not_applicable: "不适用" };

export default function ReproductionCard({ reportId }: { reportId: number }) {
  const [data, setData] = useState<Preparation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef<object | null>(null);

  useEffect(() => {
    const id = {};
    requestId.current = id;
    getReproductionCard(reportId).then(res => {
      if (id === requestId.current) { setData(res.data); setLoading(false); setError(""); }
    }).catch(() => { if (id === requestId.current) setError("读取准备卡失败，请重试。"); });
    return () => { requestId.current = null; };
  }, [reportId]);

  async function prepare() {
    const id = {};
    requestId.current = id;
    setLoading(true); setError("");
    try {
      const res = await prepareReproductionCard(reportId, data?.status === "ready");
      if (id === requestId.current) setData(res.data);
    } catch {
      if (id === requestId.current) setError("生成失败，请重试。已有准备卡仍保留。");
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }

  return <ScrollArea className="h-full"><div className="p-4 space-y-4 text-sm">
    <p className="text-muted-foreground">整理原文设置与待核查项，帮助你开始动手复现。</p>
    <Button onClick={prepare} disabled={loading}>{loading ? "正在核对原文…" : data?.status === "ready" ? "重新核对" : "生成复现准备卡"}</Button>
    <p className="text-xs text-muted-foreground">生成会调用模型；不会下载数据或运行实验。</p>
    {error && <p role="alert">{error}</p>}
    {data?.notice && <p className="text-muted-foreground">{data.notice}</p>}
    {Object.entries(data?.fields ?? {}).map(([name, fact]) => <Card key={name}>
      <CardHeader className="pb-2"><CardTitle className="text-sm">{labels[name] ?? name} · {statusLabels[fact.status]}</CardTitle></CardHeader>
      <CardContent className="space-y-2"><p className="whitespace-pre-wrap">{fact.value}</p>
        {fact.evidence.map((e, i) => <details key={i} className="text-xs text-muted-foreground"><summary>{e.locator}</summary><blockquote className="border-l-2 pl-3 mt-2 whitespace-pre-wrap">{e.raw_quote || e.quote}</blockquote></details>)}
      </CardContent>
    </Card>)}
    {data?.minimum_check && <Card><CardHeader><CardTitle className="text-sm">最小验证与否定条件</CardTitle></CardHeader><CardContent className="space-y-3">
      <p className="text-xs text-muted-foreground">{data.proposal_label}</p><p className="whitespace-pre-wrap">{data.minimum_check}</p><p className="whitespace-pre-wrap">{data.falsification}</p>
    </CardContent></Card>}
  </div></ScrollArea>;
}
