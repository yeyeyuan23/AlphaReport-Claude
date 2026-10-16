import { useState, useEffect } from "react";
import { useParams, Link } from "react-router-dom";
import { getReport } from "@/services/api";
import type { ReportDetail } from "@/types";
import {
  ResizablePanelGroup,
  ResizablePanel,
  ResizableHandle,
} from "@/components/ui/resizable";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import PaperViewer from "@/components/PaperViewer";
import ResearchBriefing from "@/components/ResearchBriefing";
import ResearchEvaluation from "@/components/ResearchEvaluation";
import KnowledgeCard from "@/components/KnowledgeCard";

export default function ReportDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [report, setReport] = useState<ReportDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    setIsMobile(mq.matches);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    getReport(Number(id)).then((res) => {
      if (res.success && res.data) setReport(res.data);
      setLoading(false);
    });
  }, [id]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground text-sm">
        加载中...
      </div>
    );
  }

  if (!report) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">研报不存在</p>
        <Link to="/library" className="text-primary text-sm mt-2 inline-block">
          返回研报库
        </Link>
      </div>
    );
  }

  return (
    <div className="h-[calc(100vh-4rem)] flex flex-col">
      {/* Top header bar */}
      <header className="flex items-center justify-between px-4 py-2 border-b bg-background shrink-0">
        <Link
          to="/library"
          className="text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          &larr; 返回研报库
        </Link>
      </header>

      {/* Split panel area */}
      <div className="flex-1 min-h-0">
        <ResizablePanelGroup
          orientation={isMobile ? "vertical" : "horizontal"}
          className="h-full"
        >
          {/* Left: PaperViewer / Research Briefing */}
          <ResizablePanel defaultSize={isMobile ? 50 : 55} minSize={isMobile ? 20 : 30}>
            <Tabs defaultValue="pdf" className="h-full flex flex-col">
              <div className="px-4 pt-2 shrink-0">
                <TabsList className="w-full">
                  <TabsTrigger value="pdf" className="flex-1 text-xs">
                    原文 PDF
                  </TabsTrigger>
                  <TabsTrigger value="briefing" className="flex-1 text-xs">
                    研报简报
                  </TabsTrigger>
                </TabsList>
              </div>
              <TabsContent
                value="pdf"
                className="flex-1 min-h-0 mt-0 data-[state=inactive]:hidden"
              >
                <PaperViewer report={report} />
              </TabsContent>
              <TabsContent
                value="briefing"
                className="flex-1 min-h-0 mt-0 overflow-y-auto data-[state=inactive]:hidden"
              >
                <ResearchEvaluation evaluation={report.evaluation} />
                {(!report.evaluation || report.analysis) && (
                  <div className="min-h-64">
                    <ResearchBriefing
                      reportId={report.id}
                      analysis={report.analysis ?? null}
                    />
                  </div>
                )}
              </TabsContent>
            </Tabs>
          </ResizablePanel>

          <ResizableHandle withHandle />

          {/* Right: Knowledge Card / Ask AI */}
          <ResizablePanel defaultSize={isMobile ? 50 : 45} minSize={isMobile ? 20 : 25}>
            <Tabs defaultValue="knowledge-card" className="h-full flex flex-col">
              <div className="px-4 pt-3">
                <TabsList className="w-full">
                  <TabsTrigger value="knowledge-card" className="flex-1 text-xs">
                    Knowledge Card
                  </TabsTrigger>
                </TabsList>
              </div>
              <TabsContent
                value="knowledge-card"
                className="flex-1 min-h-0 mt-0 data-[state=inactive]:hidden"
              >
                <KnowledgeCard reportId={report.id} />
              </TabsContent>
            </Tabs>
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>
    </div>
  );
}
