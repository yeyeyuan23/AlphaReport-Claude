import type { ResearchEvaluation as Evaluation, ResearchEvidence } from '@/types';

const TIER_STYLES: Record<Evaluation['tier'], string> = {
  focus: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  browse: 'border-indigo-200 bg-indigo-50 text-indigo-800',
  pending: 'border-amber-200 bg-amber-50 text-amber-800',
  exclude: 'border-gray-200 bg-gray-100 text-gray-600',
};

const SOURCE_CATEGORIES: Record<Evaluation['source_category'], string> = {
  academic: '学术论文',
  broker: '券商研报',
  community: '社区研究',
  other: '其他来源',
};

const RESEARCH_TYPES = {
  empirical: '实证研究',
  theoretical: '理论研究',
  methodological: '方法研究',
  broker: '券商研究',
  community: '社区研究',
};

const QUALITY_LABELS: Record<Evaluation['quality'], string> = {
  strong: '较强',
  sound: '基本扎实',
  weak: '较弱',
  unknown: '尚不确定',
};

const CONFIDENCE_LABELS: Record<Evaluation['confidence'], string> = {
  high: '高',
  medium: '中',
  low: '低',
};

export function EvaluationBadge({ evaluation }: { evaluation?: Evaluation | null }) {
  return (
    <span
      className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${
        (evaluation && TIER_STYLES[evaluation.tier]) || 'border-gray-200 bg-gray-50 text-gray-500'
      }`}
    >
      {evaluation ? evaluation.label?.trim() || '待核验' : '尚未按新标准评估'}
    </span>
  );
}

export function RelevanceLabel({ value }: { value?: number | null }) {
  if (value == null || !Number.isFinite(value)) return null;
  return (
    <span
      className="text-xs text-muted-foreground"
      title="相关度只反映研究方向的匹配程度，不代表研究质量。"
    >
      方向相关度 {Number(value.toFixed(1))}/10 · 初筛
    </span>
  );
}

function EvidenceClaim({ evidence }: { evidence: ResearchEvidence }) {
  return (
    <div className="space-y-1.5">
      <p className="whitespace-pre-line leading-relaxed">{evidence.claim}</p>
      {evidence.quote && (
        <details className="rounded-lg border border-border/70 bg-muted/30 px-3 py-2 text-xs">
          <summary className="cursor-pointer text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-2">
            查看原文引证（供核对）{evidence.chunk_id && ` · 提取文本段 ${evidence.chunk_id}`}
          </summary>
          <blockquote className="mt-3 border-l-2 border-indigo-200 pl-3 whitespace-pre-wrap break-words leading-relaxed text-foreground/80">
            {evidence.quote}
          </blockquote>
          <p className="mt-2 text-muted-foreground">
            {evidence.locator || (evidence.chunk_id && `提取文本段 ${evidence.chunk_id} · `)}
            引证供查阅，不代表研究结论已被独立验证。文本段编号不是 PDF 页码。
          </p>
        </details>
      )}
    </div>
  );
}

function EvidenceSection({ title, items }: { title: string; items: ResearchEvidence[] }) {
  if (!items.length) return null;
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      <ul className="space-y-3 text-[13px]">
        {items.map((item, index) => (
          <li key={`${item.chunk_id}-${index}`}>
            <EvidenceClaim evidence={item} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function NotesSection({
  title,
  items,
  caution = false,
}: {
  title: string;
  items: string[];
  caution?: boolean;
}) {
  if (!items.length) return null;
  return (
    <section
      className={caution ? 'rounded-lg border border-amber-200 bg-amber-50 p-3 text-amber-950' : ''}
    >
      <h3 className="text-sm font-semibold mb-2">{title}</h3>
      <ul className="list-disc pl-4 space-y-1.5 text-[13px] leading-relaxed">
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

export default function ResearchEvaluation({ evaluation }: { evaluation?: Evaluation | null }) {
  if (!evaluation) {
    return (
      <section className="border-b p-4 sm:p-5">
        <EvaluationBadge evaluation={evaluation} />
        <p className="mt-2 text-xs text-muted-foreground">
          历史分析保留在下方，新版全文评估暂无记录。
        </p>
      </section>
    );
  }

  return (
    <article className="border-b p-4 sm:p-5 space-y-5 break-words">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold">研究评估</h2>
          <EvaluationBadge evaluation={evaluation} />
          <span className="text-xs text-muted-foreground">
            {SOURCE_CATEGORIES[evaluation.source_category]}
          </span>
          {evaluation.research_type && (
            <span className="text-xs text-muted-foreground">
              {RESEARCH_TYPES[evaluation.research_type]}
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span>研究质量：{QUALITY_LABELS[evaluation.quality]}</span>
          <span>评估置信度：{CONFIDENCE_LABELS[evaluation.confidence]}</span>
          <span>{evaluation.text_complete ? '已覆盖全部提取文本' : '全文评估未完成'}</span>
        </div>
        <p className="text-xs text-muted-foreground">
          基于已保存的原文评估；质量判断仍需结合下方证据与局限阅读。
        </p>
      </header>

      {(evaluation.status !== 'reviewed' || evaluation.validation_issues.length > 0) && (
        <NotesSection
          title="评估尚未完成"
          items={
            evaluation.validation_issues.length
              ? evaluation.validation_issues
              : ['本次全文阅读评估尚未完成。']
          }
          caution
        />
      )}

      <section className="space-y-1.5">
        <RelevanceLabel value={evaluation.relevance} />
        {evaluation.relevance_reason && (
          <p className="text-xs text-muted-foreground leading-relaxed">
            {evaluation.relevance_reason}
          </p>
        )}
      </section>

      {evaluation.research_question && (
        <section>
          <h3 className="text-sm font-semibold mb-2">研究问题</h3>
          <p className="text-[13px] leading-relaxed whitespace-pre-line">
            {evaluation.research_question}
          </p>
        </section>
      )}

      <EvidenceSection title="核心方法" items={evaluation.method ? [evaluation.method] : []} />
      <EvidenceSection title="主要发现" items={evaluation.findings} />
      <EvidenceSection title="研究优点" items={evaluation.strengths} />
      <NotesSection title="局限与未解决的问题" items={evaluation.limitations} />
      <NotesSection title="关键疑虑" items={evaluation.critical_concerns} caution />

      {evaluation.reading_advice && (
        <section className="rounded-lg bg-muted/40 p-3">
          <h3 className="text-sm font-semibold mb-2">阅读建议</h3>
          <p className="text-[13px] leading-relaxed whitespace-pre-line">
            {evaluation.reading_advice}
          </p>
        </section>
      )}

      <p className="text-[11px] text-muted-foreground">
        评估时间：
        {evaluation.reviewed_at
          ? new Date(evaluation.reviewed_at).toLocaleString('zh-CN')
          : '未记录'}
        {evaluation.model && ` · 模型：${evaluation.model}`}
      </p>
    </article>
  );
}
