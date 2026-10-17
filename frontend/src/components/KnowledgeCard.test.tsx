import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import KnowledgeCard from './KnowledgeCard';
import type { KnowledgeCard as CardData } from '@/types';
import { deferred, json } from '@/test/fixtures';

function card(summary: string): CardData {
  return {
    summary,
    highlights: [],
    methodology_steps: [],
    results: '',
    marginal_contributions: '',
    implications: [],
    topics: [],
    asset_classes: [],
    quality_score: null,
    status: 'ready',
    quality_label: '已核验',
    notice: '',
    limitations: [],
    evidence: [],
  };
}

function success(summary: string) {
  return json({ success: true, data: card(summary) });
}

describe('KnowledgeCard request states', () => {
  it('reports a failed initial request instead of remaining in loading', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    const { container } = render(<KnowledgeCard reportId={1} />);
    expect(await screen.findByText('offline')).toBeVisible();
    expect(screen.getByText('论文速览加载失败')).toBeVisible();
    expect(container.querySelector('[data-slot="skeleton"]')).not.toBeInTheDocument();
  });

  it('reports an unsuccessful response with no card instead of rendering a blank panel', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(json({ success: false, error: 'Card is unavailable' })),
    );
    render(<KnowledgeCard reportId={1} />);
    expect(await screen.findByText('Card is unavailable')).toBeVisible();
    expect(screen.getByText('论文速览加载失败')).toBeVisible();
  });

  it('clears the rendered card while the next report is loading', async () => {
    const next = deferred<Response>();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(success('First summary')).mockReturnValueOnce(next.promise),
    );
    const { rerender, container } = render(<KnowledgeCard reportId={1} />);
    expect(await screen.findByText('First summary')).toBeVisible();
    rerender(<KnowledgeCard reportId={2} />);
    expect(screen.queryByText('First summary')).not.toBeInTheDocument();
    expect(container.querySelector('[data-slot="skeleton"]')).toBeInTheDocument();
    await act(async () => next.resolve(success('Second summary')));
    expect(screen.getByText('Second summary')).toBeVisible();
  });

  it('clears the previous error and recovers when another report is loaded', async () => {
    const next = deferred<Response>();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValueOnce(new Error('First error')).mockReturnValueOnce(next.promise),
    );
    const { rerender, container } = render(<KnowledgeCard reportId={1} />);
    expect(await screen.findByText('First error')).toBeVisible();
    rerender(<KnowledgeCard reportId={2} />);
    expect(screen.queryByText('First error')).not.toBeInTheDocument();
    expect(container.querySelector('[data-slot="skeleton"]')).toBeInTheDocument();
    await act(async () => next.resolve(success('Second summary')));
    expect(screen.getByText('Second summary')).toBeVisible();
  });

  it('allows only one refresh at a time and unlocks after success', async () => {
    const refresh = deferred<Response>();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(success('Original summary'))
      .mockReturnValueOnce(refresh.promise)
      .mockResolvedValueOnce(success('Third summary'));
    vi.stubGlobal('fetch', fetch);
    render(<KnowledgeCard reportId={1} />);
    const button = await screen.findByRole('button', { name: '刷新展示' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenLastCalledWith(
      '/api/v1/reports/1/knowledge-card?force=true',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(button).toBeDisabled();
    expect(screen.getByText('Original summary')).toBeVisible();
    await act(async () => refresh.resolve(success('Refreshed summary')));
    expect(screen.getByText('Refreshed summary')).toBeVisible();
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(await screen.findByText('Third summary')).toBeVisible();
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('reports an unsuccessful refresh response and stops regenerating', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(success('Original summary'))
        .mockResolvedValueOnce(json({ success: false, error: 'Refresh unavailable' })),
    );
    const { container } = render(<KnowledgeCard reportId={1} />);
    fireEvent.click(await screen.findByRole('button', { name: '刷新展示' }));
    expect(await screen.findByText('Refresh unavailable')).toBeVisible();
    expect(container.querySelector('[data-slot="skeleton"]')).not.toBeInTheDocument();
    expect(screen.queryByText('刷新中...')).not.toBeInTheDocument();
  });

  it('shows refresh errors and recovers after remounting the same report', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(success('Original summary'))
      .mockRejectedValueOnce(new Error('Refresh failed'))
      .mockResolvedValueOnce(success('Recovered summary'));
    vi.stubGlobal('fetch', fetch);
    const { unmount } = render(<KnowledgeCard reportId={1} />);
    fireEvent.click(await screen.findByRole('button', { name: '刷新展示' }));
    expect(await screen.findByText('Refresh failed')).toBeVisible();
    unmount();
    render(<KnowledgeCard reportId={1} />);
    expect(await screen.findByText('Recovered summary')).toBeVisible();
    expect(screen.queryByText('Refresh failed')).not.toBeInTheDocument();
    expect(fetch).toHaveBeenLastCalledWith(
      '/api/v1/reports/1/knowledge-card?force=false',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it.each(['success', 'failure'] as const)(
    'ignores a stale refresh %s without leaving the next report disabled',
    async (outcome) => {
      const previous = deferred<Response>();
      vi.stubGlobal(
        'fetch',
        vi
          .fn()
          .mockResolvedValueOnce(success('First summary'))
          .mockReturnValueOnce(previous.promise)
          .mockResolvedValueOnce(success('Current summary')),
      );
      const { rerender } = render(<KnowledgeCard reportId={1} />);
      fireEvent.click(await screen.findByRole('button', { name: '刷新展示' }));
      rerender(<KnowledgeCard reportId={2} />);
      expect(await screen.findByText('Current summary')).toBeVisible();
      expect(screen.getByRole('button', { name: '刷新展示' })).toBeEnabled();
      await act(async () => {
        if (outcome === 'success') previous.resolve(success('Stale summary'));
        else previous.reject(new Error('Stale error'));
      });
      expect(screen.getByText('Current summary')).toBeVisible();
      expect(screen.queryByText('Stale summary')).not.toBeInTheDocument();
      expect(screen.queryByText('Stale error')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: '刷新展示' })).toBeEnabled();
    },
  );
});
