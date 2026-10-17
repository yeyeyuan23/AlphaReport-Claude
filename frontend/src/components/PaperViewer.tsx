import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  SOURCE_LABELS,
  MARKET_LABELS,
  ASSET_LABELS,
  FREQUENCY_LABELS,
  TOPIC_LABELS,
} from '@/types';
import type { ReportDetail } from '@/types';

interface PaperViewerProps {
  report: ReportDetail;
}

export default function PaperViewer({ report }: PaperViewerProps) {
  const [pdfError, setPdfError] = useState(false);

  const pdfUrl = `/api/v1/reports/${report.id}/pdf`;

  const allTags = [
    ...report.markets.map((m) => ({
      label: '市场',
      value: MARKET_LABELS[m as keyof typeof MARKET_LABELS] || m,
    })),
    ...report.asset_classes.map((a) => ({
      label: '资产',
      value: ASSET_LABELS[a as keyof typeof ASSET_LABELS] || a,
    })),
    ...report.frequencies.map((f) => ({
      label: '频率',
      value: FREQUENCY_LABELS[f as keyof typeof FREQUENCY_LABELS] || f,
    })),
    ...report.topics.map((t) => ({
      label: '主题',
      value: TOPIC_LABELS[t as keyof typeof TOPIC_LABELS] || t,
    })),
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Metadata Header */}
      <div className="px-4 py-3 border-b shrink-0">
        <h1 className="text-base font-semibold leading-snug mb-1.5">{report.title}</h1>

        <div className="flex items-center gap-2 mb-1.5 flex-wrap">
          {report.authors.length > 0 && (
            <span className="text-sm text-muted-foreground">{report.authors.join(', ')}</span>
          )}
          <Badge variant="secondary" className="text-xs">
            {SOURCE_LABELS[report.source] || report.source}
          </Badge>
          {report.published_date && (
            <span className="text-xs text-muted-foreground">
              {report.published_date.slice(0, 10)}
            </span>
          )}
        </div>

        {allTags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {allTags.map((t, i) => (
              <Badge key={i} variant="outline" className="text-xs">
                {t.value}
              </Badge>
            ))}
          </div>
        )}
      </div>

      {/* PDF / Content Viewer */}
      <div className="flex-1 min-h-0 bg-muted/30">
        {!pdfError ? (
          <iframe
            src={pdfUrl}
            className="w-full h-full"
            title="PDF Viewer"
            onError={() => setPdfError(true)}
          />
        ) : (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3 p-8 overflow-auto">
            <span className="text-2xl">📄</span>
            <p className="text-sm">PDF 加载失败</p>
            <a href={pdfUrl} target="_blank" rel="noopener noreferrer">
              <Button variant="outline" size="sm">
                在新窗口打开 PDF
              </Button>
            </a>
            {report.abstract && (
              <div className="text-left max-w-2xl w-full">
                <h3 className="text-sm font-semibold mb-1">摘要</h3>
                <p className="text-xs leading-relaxed whitespace-pre-wrap">{report.abstract}</p>
              </div>
            )}
            {report.full_text && (
              <div className="text-left max-w-2xl w-full mt-3">
                <h3 className="text-sm font-semibold mb-1">全文</h3>
                <p className="text-xs leading-relaxed whitespace-pre-wrap line-clamp-[50]">
                  {report.full_text}
                </p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
