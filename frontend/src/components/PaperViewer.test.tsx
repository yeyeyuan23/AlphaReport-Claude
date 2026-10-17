import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import PaperViewer from './PaperViewer';
import { deferred, report } from '@/test/fixtures';

describe('PaperViewer', () => {
  it.each([
    [404, 'application/json'],
    [200, 'text/html'],
  ])('shows body without an iframe error event for PDF HTTP %s / %s', async (status, type) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('unavailable', {
      status, headers: { 'Content-Type': type },
    })));
    render(<PaperViewer report={report()} />);
    expect(await screen.findByText('论文完整正文')).toBeVisible();
    expect(screen.queryByTitle('PDF Viewer')).not.toBeInTheDocument();
  });

  it('allows switching to full body even when the PDF check succeeds', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('%PDF-1.7', {
      headers: { 'Content-Type': 'application/pdf' },
    })));
    render(<PaperViewer report={report()} />);
    expect(await screen.findByTitle('PDF Viewer')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '阅读正文' }));
    expect(screen.getByText('论文完整正文')).toBeVisible();
    expect(screen.queryByTitle('PDF Viewer')).not.toBeInTheDocument();
  });

  it('finishes with an explicit empty-content message when both sources are unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    render(<PaperViewer report={report({ abstract: '', full_text: '' })} />);
    expect(await screen.findByText('暂无可显示的正文或摘要')).toBeVisible();
    expect(screen.queryByText('正在检查 PDF...')).not.toBeInTheDocument();
  });

  it('falls back after a stalled PDF request instead of loading indefinitely', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
      render(<PaperViewer report={report({ abstract: '', full_text: '' })} />);
      expect(screen.getByRole('status')).toHaveTextContent('正在检查 PDF...');
      await act(async () => { await vi.advanceTimersByTimeAsync(15001); });
      expect(screen.getByText('暂无可显示的正文或摘要')).toBeVisible();
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('falls back when a successful PDF response has no bytes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', {
      headers: { 'Content-Type': 'application/pdf' },
    })));
    render(<PaperViewer report={report()} />);
    expect(await screen.findByText('论文完整正文')).toBeVisible();
    expect(screen.queryByTitle('PDF Viewer')).not.toBeInTheDocument();
  });

  it('ignores a prior report PDF check that completes after the report changes', async () => {
    const old = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn((url: string) => url.includes('/1/') ? old.promise : Promise.resolve(
      new Response('missing', { status: 404 }),
    )));
    const { rerender } = render(<PaperViewer report={report()} />);
    rerender(<PaperViewer report={report({ id: 2, full_text: '第二篇正文' })} />);
    expect(await screen.findByText('第二篇正文')).toBeVisible();
    await act(async () => old.resolve(new Response('%PDF', { headers: { 'Content-Type': 'application/pdf' } })));
    expect(screen.getByText('第二篇正文')).toBeVisible();
    expect(screen.queryByTitle('PDF Viewer')).not.toBeInTheDocument();
  });
});
