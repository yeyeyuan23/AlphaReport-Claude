import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import ReportLibraryPage from './ReportLibraryPage';
import { deferred, json, report } from '@/test/fixtures';

function list(title: string) {
  return json({
    success: true,
    data: { reports: [report({ title })], total: 1, limit: 20, offset: 0 },
  });
}

describe('ReportLibraryPage', () => {
  it.each([
    [
      { published_date: null, publication_date_info: { precision: 'month', year: 2024, month: 2 } },
      '发表：2024-02（精确到月）',
    ],
    [
      { published_date: null, publication_date_info: { precision: 'year', year: 2024 } },
      '发表：2024（精确到年）',
    ],
    [{ published_date: '2024-02-03T00:00:00' }, '发表：2024-02-03'],
    [{ published_date: null, announced_at: '2026-10-16T04:00:00Z' }, '公告：2026-10-16'],
    [{ published_date: null }, '发表日期未知 · 入库：2026-10-17'],
    [
      { published_date: '2024-02-03T00:00:00', announced_at: '2026-10-16T04:00:00Z' },
      '公告：2026-10-16 · 发表：2024-02-03',
    ],
    [
      {
        published_date: '2024-02-01T00:00:00',
        publication_date_info: { precision: 'month', year: 2024, month: 2 },
      },
      '发表：2024-02（精确到月）',
    ],
    [
      { published_date: '2024-01-01T00:00:00', publication_date_info: { precision: 'unknown' } },
      '发表日期未知 · 入库：2026-10-17',
    ],
  ])('shows truthful date metadata %#', async (metadata, expected) => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        Promise.resolve(
          json(
            url.includes('/stats')
              ? { success: false }
              : {
                  success: true,
                  data: {
                    reports: [{ ...report(), created_at: '2026-10-17T01:23:45', ...metadata }],
                    total: 1,
                    limit: 20,
                    offset: 0,
                  },
                },
          ),
        ),
      ),
    );
    render(
      <MemoryRouter>
        <ReportLibraryPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText(expected)).toBeVisible();
  });

  it('retains the newest search result when the old request finishes last', async () => {
    const old = deferred<Response>();
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/stats')) return Promise.resolve(json({ success: false }));
        if (url.includes('search=new')) return Promise.resolve(list('新结果'));
        return old.promise;
      }),
    );
    render(
      <MemoryRouter>
        <ReportLibraryPage />
      </MemoryRouter>,
    );
    fireEvent.change(screen.getByPlaceholderText('搜索标题或摘要...'), {
      target: { value: 'new' },
    });
    expect(await screen.findByText('新结果')).toBeVisible();
    await act(async () => old.resolve(list('旧结果')));
    expect(screen.getByText('新结果')).toBeVisible();
    expect(screen.queryByText('旧结果')).not.toBeInTheDocument();
  });

  it('does not end the newest loading state when an old request completes', async () => {
    const old = deferred<Response>();
    const latest = deferred<Response>();
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/stats')) return Promise.resolve(json({ success: false }));
        return url.includes('search=new') ? latest.promise : old.promise;
      }),
    );
    render(
      <MemoryRouter>
        <ReportLibraryPage />
      </MemoryRouter>,
    );
    fireEvent.change(screen.getByPlaceholderText('搜索标题或摘要...'), {
      target: { value: 'new' },
    });
    await act(async () => old.resolve(list('旧结果')));
    expect(screen.getByText('加载中...')).toBeVisible();
    expect(screen.queryByText('旧结果')).not.toBeInTheDocument();
    await act(async () => latest.resolve(list('新结果')));
    expect(screen.getByText('新结果')).toBeVisible();
  });

  it('shows a request error and exits loading on a failed search', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        url.includes('/stats')
          ? Promise.resolve(json({ success: false }))
          : Promise.reject(new Error('offline')),
      ),
    );
    render(
      <MemoryRouter>
        <ReportLibraryPage />
      </MemoryRouter>,
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('研报加载失败');
    expect(screen.queryByText('加载中...')).not.toBeInTheDocument();
    expect(screen.queryByText('暂无研报')).not.toBeInTheDocument();
  });
});
