import type { ReportDetail } from '@/types';

export function report(overrides: Partial<ReportDetail> = {}): ReportDetail {
  return {
    id: 1, title: '论文一', authors: [], abstract: '论文摘要',
    source: 'arxiv', has_full_text: true, full_text: '论文完整正文',
    markets: [], asset_classes: [], frequencies: [], topics: [], custom_tags: [],
    content_hash: 'test-hash', created_at: '2026-10-17', updated_at: '2026-10-17',
    ...overrides,
  };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

export function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}
