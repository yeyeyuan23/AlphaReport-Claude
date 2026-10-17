import type { ReportDetail } from '@/types';

export function report(id: number, title: string): ReportDetail {
  return {
    id,
    title,
    authors: ['Researcher'],
    abstract: 'Research abstract',
    source: 'arxiv',
    has_full_text: true,
    markets: [],
    asset_classes: [],
    frequencies: [],
    topics: [],
    custom_tags: [],
    content_hash: `hash-${id}`,
    created_at: '2026-10-16T00:00:00Z',
    updated_at: '2026-10-16T00:00:00Z',
    full_text: 'Full paper text',
  };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
