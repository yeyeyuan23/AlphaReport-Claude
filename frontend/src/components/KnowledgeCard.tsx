import { useState, useEffect, useRef, useCallback } from 'react';
import Markdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';

const markdownPlugins = { remarkPlugins: [remarkMath], rehypePlugins: [rehypeKatex] };
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Skeleton } from '@/components/ui/skeleton';
import { getKnowledgeCard } from '@/services/api';
import type { KnowledgeCard as KnowledgeCardType } from '@/types';

function SectionCard({
  title,
  icon,
  children,
}: {
  title: string;
  icon: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="border-border/60">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium flex items-center gap-1.5">
          <span>{icon}</span>
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="text-sm">{children}</CardContent>
    </Card>
  );
}

export default function KnowledgeCard({ reportId }: { reportId: number }) {
  return <KnowledgeCardContent key={reportId} reportId={reportId} />;
}

function KnowledgeCardContent({ reportId }: { reportId: number }) {
  const [data, setData] = useState<KnowledgeCardType | null>(null);
  const [loading, setLoading] = useState(true);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef<object | null>(null);

  const receiveCard = useCallback((request: ReturnType<typeof getKnowledgeCard>, id: object) => {
    return request
      .then((res) => {
        if (requestId.current !== id) return;
        if (res.success && res.data) {
          setData(res.data);
        } else {
          setError((res as { error?: string }).error || 'Failed to load knowledge card');
        }
      })
      .catch((e: Error) => {
        if (requestId.current === id) setError(e.message);
      })
      .finally(() => {
        if (requestId.current === id) {
          setLoading(false);
          setRegenerating(false);
        }
      });
  }, []);

  const refreshCard = () => {
    const id = {};
    requestId.current = id;
    setRegenerating(true);
    setError(null);
    void receiveCard(getKnowledgeCard(reportId, true), id);
  };

  useEffect(() => {
    const id = {};
    requestId.current = id;
    void receiveCard(getKnowledgeCard(reportId), id);
    return () => {
      requestId.current = null;
    };
  }, [reportId, receiveCard]);

  if (loading && !data) {
    return (
      <div className="space-y-3 p-4">
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 text-center text-muted-foreground text-sm">
        <p>论文速览加载失败</p>
        <p className="text-xs mt-1">{error}</p>
      </div>
    );
  }

  if (!data) return null;

  return (
    <ScrollArea className="h-full">
      <div className="p-4 sm:p-5 space-y-4">
        {/* Topic & Asset Tags */}
        <div className="flex flex-wrap gap-1.5 items-center">
          {data.topics.length > 0 || data.asset_classes.length > 0 ? (
            <>
              {data.topics.map((t) => (
                <Badge key={t} variant="secondary" className="text-xs">
                  {t}
                </Badge>
              ))}
              {data.asset_classes.map((a) => (
                <Badge key={a} variant="outline" className="text-xs">
                  {a}
                </Badge>
              ))}
            </>
          ) : null}
          <Badge variant="outline" className="text-xs ml-auto">
            {data.quality_label}
          </Badge>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground"
            onClick={refreshCard}
            disabled={regenerating}
          >
            {regenerating ? '刷新中...' : '刷新展示'}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">{data.notice}</p>

        {/* Summary */}
        {data.summary && (
          <SectionCard title={data.status === 'ready' ? '研究问题' : '原始摘要'} icon="&#x1F4DD;">
            <div className="prose prose-sm max-w-none text-muted-foreground leading-relaxed">
              <Markdown {...markdownPlugins}>{data.summary}</Markdown>
            </div>
          </SectionCard>
        )}

        {/* Highlights */}
        {data.highlights.length > 0 && (
          <SectionCard title="关键发现" icon="&#x1F4A1;">
            <ul className="space-y-2">
              {data.highlights.map((h, i) => (
                <li key={i} className="flex items-start gap-2">
                  <span className="mt-0.5 h-1.5 w-1.5 rounded-full bg-primary shrink-0" />
                  <Markdown {...markdownPlugins}>{h}</Markdown>
                </li>
              ))}
            </ul>
          </SectionCard>
        )}

        {/* Methodology Steps */}
        {data.methodology_steps.length > 0 && (
          <SectionCard title="方法步骤" icon="&#x1F9EA;">
            <ol className="space-y-3 list-decimal list-inside">
              {data.methodology_steps.map((s, i) => (
                <li key={i} className="text-muted-foreground pl-1">
                  <Markdown {...markdownPlugins}>{s}</Markdown>
                </li>
              ))}
            </ol>
          </SectionCard>
        )}

        {/* Results */}
        {data.results && (
          <SectionCard title="研究结果与条件" icon="&#x1F4CA;">
            <div className="prose prose-sm max-w-none text-muted-foreground leading-relaxed">
              <Markdown {...markdownPlugins}>{data.results}</Markdown>
            </div>
          </SectionCard>
        )}

        {/* Marginal Contributions */}
        {data.marginal_contributions && (
          <SectionCard title="边际贡献" icon="&#x1F3AF;">
            <div className="bg-primary/5 border border-primary/10 rounded-lg p-3 text-sm text-muted-foreground leading-relaxed">
              <Markdown {...markdownPlugins}>{data.marginal_contributions}</Markdown>
            </div>
          </SectionCard>
        )}

        {/* Implications */}
        {data.implications.length > 0 && (
          <SectionCard title="阅读与验证建议" icon="&#x1F52E;">
            <div className="flex flex-col gap-2">
              {data.implications.map((imp, i) => (
                <div
                  key={i}
                  className="bg-secondary/60 rounded-lg px-3 py-2 text-xs text-muted-foreground leading-relaxed"
                >
                  <Markdown {...markdownPlugins}>{imp}</Markdown>
                </div>
              ))}
            </div>
          </SectionCard>
        )}

        {data.limitations?.length > 0 && (
          <SectionCard title="局限与待核查项" icon="🔎">
            <ul className="list-disc pl-4 space-y-2">
              {data.limitations.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ul>
          </SectionCard>
        )}
        {data.evidence?.length > 0 && (
          <SectionCard title="原文引证（供核对）" icon="📖">
            <div className="space-y-3">
              {data.evidence.map((e, i) => (
                <details key={i}>
                  <summary className="cursor-pointer">{e.claim}</summary>
                  <p className="text-xs text-muted-foreground mt-2">{e.locator}</p>
                  <blockquote className="border-l-2 pl-3 mt-2 text-xs whitespace-pre-wrap">
                    {e.raw_quote || e.quote}
                  </blockquote>
                </details>
              ))}
            </div>
          </SectionCard>
        )}

        {/* Empty state */}
        {!data.summary && data.highlights.length === 0 && data.methodology_steps.length === 0 && (
          <div className="text-center text-muted-foreground py-12">
            <p className="text-sm">暂无结构化分析数据</p>
            <p className="text-xs mt-1">请先完成全文阅读评估。</p>
          </div>
        )}
      </div>
    </ScrollArea>
  );
}
