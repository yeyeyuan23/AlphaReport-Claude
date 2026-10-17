import { useState, useEffect } from 'react';
import Markdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';

const mdPlugins = { remarkPlugins: [remarkMath], rehypePlugins: [rehypeKatex] };
import { ScrollArea } from '@/components/ui/scroll-area';
import { Skeleton } from '@/components/ui/skeleton';
import { getResearchBriefing } from '@/services/api';
import type { BriefingSection } from '@/types';

// ── Component ──

interface Props {
  reportId: number;
  briefingSections?: BriefingSection[];
}

export default function ResearchBriefing({ reportId, briefingSections }: Props) {
  const [sections, setSections] = useState<BriefingSection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);

      // Detail already includes the canonical server presentation, including an
      // authoritative empty result. Older callers can use the same API mapper.
      if (briefingSections !== undefined) {
        setSections(briefingSections);
        setLoading(false);
        return;
      }

      try {
        const res = await getResearchBriefing(reportId);
        if (!cancelled) {
          if (res.success && res.data?.sections?.length) {
            setSections(res.data.sections);
          } else if (res.success && res.data?.content) {
            // Single content block (LLM-generated)
            setSections([{ title: '研报简报', icon: '📋', content: res.data.content }]);
          } else {
            setError('暂无分析数据，请先运行 AI 分析');
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
    return () => {
      cancelled = true;
    };
  }, [reportId, briefingSections]);

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
            {idx < sections.length - 1 && <div className="mt-4 border-b border-border/40" />}
          </section>
        ))}
      </div>
    </ScrollArea>
  );
}
