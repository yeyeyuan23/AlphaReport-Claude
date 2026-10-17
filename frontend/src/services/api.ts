import type {
  ReportSummary,
  ReportDetail,
  ReportStats,
  KnowledgeCard,
  ReproductionCard,
} from '../types';

const BASE = '/api/v1';

export class RequestError extends Error {
  status: number;
  constructor(status: number) {
    super(`Request failed (${status})`);
    this.status = status;
  }
}

async function request<T>(
  url: string,
  options?: RequestInit,
): Promise<{ success: boolean; data: T }> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) throw new RequestError(res.status);
  return res.json();
}

// ── Report List ──

export interface ReportListParams {
  market?: string;
  asset_class?: string;
  frequency?: string;
  topic?: string;
  search?: string;
  source?: string;
  has_full_text?: boolean;
  sort_by?: string;
  limit?: number;
  offset?: number;
}

export async function getReports(params: ReportListParams = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
  });
  return request<{ reports: ReportSummary[]; total: number; limit: number; offset: number }>(
    `${BASE}/reports?${qs.toString()}`,
  );
}

// ── Report Detail ──

export async function getReport(id: number) {
  return request<ReportDetail>(`${BASE}/reports/${id}`);
}

// ── Stats ──

export async function getReportStats() {
  return request<ReportStats>(`${BASE}/reports/stats`);
}

export interface ResearchBriefingResult {
  sections?: { title: string; icon: string; content: string }[];
  content?: string;
  source: string;
  error?: string;
}

export function getKnowledgeCard(reportId: number, force = false) {
  return request<KnowledgeCard>(`${BASE}/reports/${reportId}/knowledge-card?force=${force}`, {
    method: 'POST',
  });
}

export function getResearchBriefing(reportId: number, force = false) {
  return request<ResearchBriefingResult>(
    `${BASE}/reports/${reportId}/research-briefing?force=${force}`,
    { method: 'POST' },
  );
}

export function getReproductionCard(reportId: number) {
  return request<ReproductionCard>(`${BASE}/reports/${reportId}/reproduction-card`);
}

export function prepareReproductionCard(reportId: number, force = false) {
  return request<ReproductionCard>(`${BASE}/reports/${reportId}/reproduction-card?force=${force}`, {
    method: 'POST',
  });
}
