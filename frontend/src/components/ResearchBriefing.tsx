import { useState, useEffect } from "react";
import Markdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

const mdPlugins = { remarkPlugins: [remarkMath], rehypePlugins: [rehypeKatex] };
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { getResearchBriefing } from "@/services/api";
import type { AnalysisResult } from "@/types";

// ── Map analysis data to narrative briefing sections ──

interface BriefingSection {
  title: string;
  icon: string;
  content: string;
}

function buildNarrativeFromAnalysis(analysis: AnalysisResult): BriefingSection[] {
  const sections: BriefingSection[] = [];

  // Section 1: 研究背景与问题 — Hook + Problem
  const problemParts: string[] = [];
  if (analysis.research_question) {
    problemParts.push(`**研究问题：** ${analysis.research_question}`);
  }
  if (analysis.core_contribution) {
    problemParts.push(`**核心创新：** ${analysis.core_contribution}`);
  }
  if (analysis.data_used?.market || analysis.data_used?.sample_period) {
    const d = analysis.data_used;
    const parts: string[] = [];
    if (d.market) parts.push(`市场：${d.market}`);
    if (d.sample_period) parts.push(`样本期：${d.sample_period}`);
    if (d.frequency) parts.push(`频率：${d.frequency}`);
    if (d.universe) parts.push(`股票池：${d.universe}`);
    if (d.instruments?.length) parts.push(`工具：${d.instruments.join("、")}`);
    problemParts.push(`**数据：** ${parts.join(" | ")}`);
  }
  if (analysis.benchmark_models?.length) {
    problemParts.push(`**基准模型：** ${analysis.benchmark_models.join("、")}`);
  }
  if (problemParts.length) {
    sections.push({
      title: "研究背景与问题",
      icon: "🎯",
      content: problemParts.join("\n\n"),
    });
  }

  // Section 2: 核心方法 — Methodology walkthrough with formulas
  const methodParts: string[] = [];
  if (analysis.methodology) {
    const m = analysis.methodology;

    // Analysis points (peer-review style critique)
    if (m.analysis_points?.length) {
      for (const pt of m.analysis_points) {
        if (typeof pt === "object" && pt !== null) {
          const p = pt as Record<string, unknown>;
          const title = (p.title as string) || "";
          const analysis = (p.analysis as string) || "";
          if (analysis) {
            methodParts.push(`### ${title}\n\n${analysis}`);
          }
        }
      }
    }

    // Factor list
    if (m.factor_list?.length) {
      const factorLines = m.factor_list.map(
        (f) =>
          `- **${f.name}**（${f.type}）：${f.construction}${f.raw_or_neutralized ? ` [${f.raw_or_neutralized}]` : ""}`
      );
      methodParts.push(`**因子清单：**\n${factorLines.join("\n")}`);
    }

    // Model architecture
    if (m.model_architecture) {
      const ma = m.model_architecture;
      const archLines = [
        `**模型架构：** ${ma.type}`,
        ma.layers_or_structure ? `- 结构：${ma.layers_or_structure}` : "",
        ma.loss_function ? `- 损失函数：$${ma.loss_function}$` : "",
        ma.regularization ? `- 正则化：${ma.regularization}` : "",
        ma.training_scheme ? `- 训练方式：${ma.training_scheme}` : "",
      ].filter(Boolean);
      methodParts.push(archLines.join("\n"));
    }

    // Portfolio construction
    if (m.portfolio_construction) {
      const pc = m.portfolio_construction;
      const portLines = [
        `**组合构建：**`,
        pc.weighting ? `- 加权方式：${pc.weighting}` : "",
        pc.rebalance_frequency ? `- 再平衡频率：${pc.rebalance_frequency}` : "",
        pc.constraints ? `- 约束条件：${pc.constraints}` : "",
        pc.transaction_cost_model ? `- 交易成本：${pc.transaction_cost_model}` : "",
      ].filter(Boolean);
      methodParts.push(portLines.join("\n"));
    }
  }

  // Key formulas
  if (analysis.equations?.length) {
    const keyEqs = analysis.equations.filter((e) => e.is_key_formula);
    if (keyEqs.length > 0) {
      const eqLines = keyEqs.map(
        (e) => `**核心公式：**\n$$${e.latex}$$\n\n${e.meaning}`
      );
      methodParts.push(`---\n\n${eqLines.join("\n\n")}`);
    }
  }

  if (methodParts.length) {
    sections.push({
      title: "核心方法",
      icon: "🔬",
      content: methodParts.join("\n\n"),
    });
  }

  // Section 3: 关键创新点 — Key concepts deep dive
  const innovParts: string[] = [];
  if (analysis.assessment?.key_contributions?.length) {
    innovParts.push(
      `**核心贡献：**\n${analysis.assessment.key_contributions.map((c) => `- ${c}`).join("\n")}`
    );
  }
  if (analysis.assessment?.marginal_contribution_summary) {
    innovParts.push(
      `**边际贡献：**\n${analysis.assessment.marginal_contribution_summary}`
    );
  }
  if (innovParts.length) {
    sections.push({
      title: "关键创新点",
      icon: "💡",
      content: innovParts.join("\n\n"),
    });
  }

  // Section 4: 实证检验 — Empirical results
  const empParts: string[] = [];
  if (analysis.assessment) {
    const a = analysis.assessment;
    if (a.overall_quality_score != null) {
      empParts.push(`**综合评分：** ${(a.overall_quality_score * 10).toFixed(1)} / 10`);
    }
    if (a.strengths?.length) {
      empParts.push(`**优势：**\n${a.strengths.map((s) => `- ${s}`).join("\n")}`);
    }
    if (a.weaknesses?.length) {
      empParts.push(`**不足：**\n${a.weaknesses.map((w) => `- ${w}`).join("\n")}`);
    }

    // Bias risks
    const biasLabels: Record<string, string> = {
      none: "无风险 ✅",
      low: "低风险 ⚠️",
      medium: "中风险 ⚠️",
      high: "高风险 ❌",
      unknown: "未知 ❓",
    };
    const br = a.bias_risks;
    if (br) {
      const biasLines = [
        `- 前视偏差：${biasLabels[br.look_ahead_bias] || br.look_ahead_bias}`,
        `- 幸存者偏差：${biasLabels[br.survivorship_bias] || br.survivorship_bias}`,
        `- 数据窥探：${biasLabels[br.data_snooping] || br.data_snooping}`,
        br.overfitting_concern && br.overfitting_concern !== "none"
          ? `- 过拟合风险：${br.overfitting_concern}`
          : "",
      ].filter(Boolean);
      empParts.push(`**偏差风险评估：**\n${biasLines.join("\n")}`);
    }

    // Reproducibility
    if (a.reproducibility) {
      const r = a.reproducibility;
      empParts.push(
        `**可复现性：** ${r.level}${r.has_data_source ? " · 有数据源" : ""}${r.has_code_available ? " · 有代码" : ""}`
      );
    }

    // A-share applicability
    if (a.a_share_applicability) {
      const as = a.a_share_applicability;
      empParts.push(
        `**A股适用性：** ${as.directly_applicable ? "可直接应用 ✅" : "需要适配 ⚠️"}`
      );
      if (as.adaptations_needed?.length) {
        empParts.push(`适配需求：${as.adaptations_needed.join("；")}`);
      }
      if (as.key_constraints?.length) {
        empParts.push(`关键限制：${as.key_constraints.join("；")}`);
      }
    }
  }
  if (empParts.length) {
    sections.push({
      title: "实证检验与评估",
      icon: "📊",
      content: empParts.join("\n\n"),
    });
  }

  // Section 5: 实践启示 — Practical implications
  if (analysis.assessment?.practical_implications?.length) {
    sections.push({
      title: "实践启示",
      icon: "🔮",
      content: analysis.assessment.practical_implications
        .map((imp, i) => `${i + 1}. ${imp}`)
        .join("\n\n"),
    });
  }

  // Section 6: 一句话总结 — Takeaway
  if (analysis.summary) {
    sections.push({
      title: "总结",
      icon: "📌",
      content: analysis.summary,
    });
  }

  return sections;
}

// ── Component ──

interface Props {
  reportId: number;
  analysis?: AnalysisResult | null;
}

export default function ResearchBriefing({ reportId, analysis }: Props) {
  const [sections, setSections] = useState<BriefingSection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);

      // 1) Use passed analysis data (client-side mapping)
      if (analysis?.research_question || analysis?.methodology || analysis?.assessment) {
        if (!cancelled) {
          setSections(buildNarrativeFromAnalysis(analysis));
          setLoading(false);
        }
        return;
      }

      // 2) Fetch from API (server-side LLM generation)
      try {
        const res = await getResearchBriefing(reportId);
        if (!cancelled) {
          if (res.success && res.data?.sections?.length) {
            setSections(res.data.sections);
          } else if (res.success && res.data?.content) {
            // Single content block (LLM-generated)
            setSections([{ title: "研报简报", icon: "📋", content: res.data.content }]);
          } else {
            setError("暂无分析数据，请先运行 AI 分析");
          }
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled) {
          setError((e as Error).message);
          setLoading(false);
        }
      }
    }

    load();
    return () => { cancelled = true; };
  }, [reportId, analysis]);

  if (loading) {
    return (
      <div className="space-y-3 p-4">
        <Skeleton className="h-5 w-2/3" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-5 w-1/2" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-5 w-3/4" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3 p-8">
        <span className="text-3xl">📄</span>
        <p className="text-sm">{error}</p>
      </div>
    );
  }

  if (!sections.length) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3 p-8">
        <span className="text-3xl">📋</span>
        <p className="text-sm">暂无研报简报数据</p>
        <p className="text-xs">请先运行 AI 深度分析以生成简报</p>
      </div>
    );
  }

  return (
    <ScrollArea className="h-full">
      <div className="p-4 sm:p-5 space-y-5">
        {/* Section cards */}
        {sections.map((section, idx) => (
          <section key={idx}>
            <h3 className="text-sm font-semibold mb-2 flex items-center gap-1.5 text-foreground/90">
              <span>{section.icon}</span>
              {section.title}
            </h3>
            <div className="prose prose-sm max-w-none text-muted-foreground leading-relaxed text-[13px]">
              <Markdown {...mdPlugins}>{section.content}</Markdown>
            </div>
            {idx < sections.length - 1 && (
              <div className="mt-4 border-b border-border/40" />
            )}
          </section>
        ))}
      </div>
    </ScrollArea>
  );
}
