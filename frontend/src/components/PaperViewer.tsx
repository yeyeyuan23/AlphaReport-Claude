import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  SOURCE_LABELS,
  MARKET_LABELS,
  ASSET_LABELS,
  FREQUENCY_LABELS,
  TOPIC_LABELS,
} from "@/types";
import type { ReportDetail } from "@/types";

interface PaperViewerProps {
  report: ReportDetail;
}

export default function PaperViewer({ report }: PaperViewerProps) {
  return <PaperViewerContent key={report.id} report={report} />;
}

function PaperViewerContent({ report }: PaperViewerProps) {
  const [pdf, setPdf] = useState<{ status: "checking" | "ready" | "unavailable"; url?: string }>({ status: "checking" });
  const [readBody, setReadBody] = useState(false);
  const pdfUrl = `/api/v1/reports/${report.id}/pdf`;
  const hasBody = Boolean(report.abstract?.trim() || report.full_text?.trim());

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let objectUrl: string | undefined;
    const timeout = window.setTimeout(() => {
      controller.abort();
      if (active) setPdf({ status: "unavailable" });
    }, 15000);

    async function loadPdf() {
      try {
        const response = await fetch(pdfUrl, { signal: controller.signal });
        const mediaType = response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase();
        if (!response.ok || mediaType !== "application/pdf") {
          await response.body?.cancel();
          throw new Error("PDF unavailable");
        }
        const blob = await response.blob();
        if (!blob.size) throw new Error("Empty PDF");
        if (!active || controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setPdf({ status: "ready", url: objectUrl });
      } catch {
        if (active) setPdf({ status: "unavailable" });
      } finally {
        window.clearTimeout(timeout);
      }
    }
    void loadPdf();
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [pdfUrl]);

  const allTags = [
    ...report.markets.map((m) => ({
      label: "市场",
      value: MARKET_LABELS[m as keyof typeof MARKET_LABELS] || m,
    })),
    ...report.asset_classes.map((a) => ({
      label: "资产",
      value: ASSET_LABELS[a as keyof typeof ASSET_LABELS] || a,
    })),
    ...report.frequencies.map((f) => ({
      label: "频率",
      value: FREQUENCY_LABELS[f as keyof typeof FREQUENCY_LABELS] || f,
    })),
    ...report.topics.map((t) => ({
      label: "主题",
      value: TOPIC_LABELS[t as keyof typeof TOPIC_LABELS] || t,
    })),
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Metadata Header */}
      <div className="px-4 py-3 border-b shrink-0">
        <h1 className="text-base font-semibold leading-snug mb-1.5">
          {report.title}
        </h1>

        <div className="flex items-center gap-2 mb-1.5 flex-wrap">
          {report.authors.length > 0 && (
            <span className="text-sm text-muted-foreground">
              {report.authors.join(", ")}
            </span>
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

      <div className="flex items-center gap-2 px-4 py-2 border-b shrink-0">
        {hasBody && (
          <Button variant="outline" size="sm" onClick={() => setReadBody(!readBody)}
            disabled={readBody && pdf.status !== "ready"}>
            {readBody ? "查看 PDF" : "阅读正文"}
          </Button>
        )}
        <Button asChild variant="outline" size="sm">
          <a href={pdfUrl} target="_blank" rel="noopener noreferrer">在新窗口打开 PDF</a>
        </Button>
      </div>

      <div className="flex-1 min-h-0 bg-muted/30">
        {!readBody && pdf.status === "ready" ? (
          <iframe src={pdf.url} className="w-full h-full" title="PDF Viewer" />
        ) : !readBody && pdf.status === "checking" ? (
          <div role="status" className="p-8 text-sm text-muted-foreground">正在检查 PDF...</div>
        ) : (
          <div className="h-full p-6 overflow-auto text-sm">
            {pdf.status === "unavailable" && (
              <p className="text-muted-foreground mb-4">PDF 暂时无法显示</p>
            )}
            {!hasBody && <p className="text-muted-foreground">暂无可显示的正文或摘要</p>}
            {report.abstract?.trim() && (
              <section className="max-w-2xl mx-auto mb-5">
                <h3 className="font-semibold mb-2">摘要</h3>
                <p className="leading-relaxed whitespace-pre-wrap">{report.abstract}</p>
              </section>
            )}
            {report.full_text?.trim() && (
              <section className="max-w-2xl mx-auto">
                <h3 className="font-semibold mb-2">全文</h3>
                <p className="leading-relaxed whitespace-pre-wrap">{report.full_text}</p>
              </section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
