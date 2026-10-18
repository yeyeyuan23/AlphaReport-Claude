import { act, fireEvent, render, screen } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import ReportDetailPage from './ReportDetailPage';
import { deferred, json, report } from '@/test/fixtures';

describe('ReportDetailPage', () => {
  it('exits loading and reports a failed detail request', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    render(
      <MemoryRouter initialEntries={['/library/1']}>
        <Routes>
          <Route path="/library/:id" element={<ReportDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('研报加载失败');
    expect(screen.queryByText('加载中...')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: '返回研报库' })).toBeVisible();
  });

  it('ignores a previous report response after navigating to a missing report', async () => {
    const old = deferred<Response>();
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        url.endsWith('/1') ? old.promise : Promise.resolve(json({ detail: 'Not found' }, 404)),
      ),
    );
    render(
      <MemoryRouter initialEntries={['/library/1']}>
        <Link to="/library/2">下一篇</Link>
        <Routes>
          <Route path="/library/:id" element={<ReportDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('link', { name: '下一篇' }));
    expect(await screen.findByText('研报不存在')).toBeVisible();
    expect(screen.queryByText('加载中...')).not.toBeInTheDocument();
    await act(async () => old.resolve(json({ success: true, data: report() })));
    expect(screen.getByText('研报不存在')).toBeVisible();
    expect(screen.queryByText('论文一')).not.toBeInTheDocument();
  });
});
