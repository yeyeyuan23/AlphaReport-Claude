import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ReproductionCard from './ReproductionCard';
import type { ReproductionCard as CardData } from '@/types';
import { deferred, json } from '@/test/fixtures';

function ready(value: string): CardData {
  return {
    status: 'ready',
    notice: '已核对原文',
    fields: { target: { status: 'documented', value, evidence: [] } },
    open_questions: ['Check data availability'],
  };
}

function success(data: CardData = { status: 'pending', notice: '尚未生成' }) {
  return json({ success: true, data });
}

describe('ReproductionCard request states', () => {
  it('allows generation after an initial read fails and clears the read error', async () => {
    const generation = deferred<Response>();
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockReturnValueOnce(generation.promise);
    vi.stubGlobal('fetch', fetch);
    render(<ReproductionCard reportId={1} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('读取准备卡失败，请重试。');
    fireEvent.click(screen.getByRole('button', { name: '生成复现准备卡' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('正在读取全文');
    expect(fetch).toHaveBeenLastCalledWith(
      '/api/v1/reports/1/reproduction-card?force=false',
      expect.objectContaining({ method: 'POST' }),
    );
    await act(async () => generation.resolve(success(ready('Recovered target'))));
    expect(screen.getByText('Recovered target')).toBeVisible();
    expect(screen.getByRole('button', { name: '重新核对' })).toBeEnabled();
  });

  it('reports an unsuccessful initial read instead of silently presenting an empty card', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ success: false })));
    render(<ReproductionCard reportId={1} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('读取准备卡失败，请重试。');
    expect(screen.getByRole('button', { name: '生成复现准备卡' })).toBeEnabled();
  });

  it('blocks repeated generation clicks and retries a failed generation without discarding the saved card', async () => {
    const generation = deferred<Response>();
    const retry = deferred<Response>();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(success(ready('Saved target')))
      .mockReturnValueOnce(generation.promise)
      .mockReturnValueOnce(retry.promise);
    vi.stubGlobal('fetch', fetch);
    render(<ReproductionCard reportId={1} />);
    expect(await screen.findByText('Saved target')).toBeVisible();
    const button = screen.getByRole('button', { name: '重新核对' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenLastCalledWith(
      '/api/v1/reports/1/reproduction-card?force=true',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(button).toBeDisabled();
    await act(async () => generation.reject(new Error('offline')));
    expect(screen.getByRole('alert')).toHaveTextContent('生成失败，请重试。已有准备卡仍保留。');
    expect(screen.getByText('Saved target')).toBeVisible();
    expect(button).toBeEnabled();
    fireEvent.click(button);
    fireEvent.click(button);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(button).toBeDisabled();
    await act(async () => retry.resolve(success(ready('Updated target'))));
    expect(screen.getByText('Updated target')).toBeVisible();
    expect(screen.queryByText('Saved target')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('准备卡已生成，共 1 项设置，1 项待核查。');
    expect(button).toBeEnabled();
  });

  it('explains a rejected generation, preserves a saved card, and permits a retry', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(success(ready('Saved target')))
        .mockResolvedValueOnce(success({ status: 'retryable_error', notice: '原文引证需要核查' }))
        .mockResolvedValueOnce(success(ready('Verified target'))),
    );
    render(<ReproductionCard reportId={1} />);
    fireEvent.click(await screen.findByRole('button', { name: '重新核对' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('原文引证需要核查');
    expect(screen.getByText('Saved target')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '重新核对' }));
    expect(await screen.findByText('Verified target')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('clears the previous card and success feedback as soon as the report changes', async () => {
    const next = deferred<Response>();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(success())
      .mockResolvedValueOnce(success(ready('First target')))
      .mockReturnValueOnce(next.promise)
      .mockResolvedValueOnce(success(ready('Second generated target')));
    vi.stubGlobal('fetch', fetch);
    const { rerender } = render(<ReproductionCard reportId={1} />);
    expect(await screen.findByText('尚未生成')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '生成复现准备卡' }));
    expect(await screen.findByText('First target')).toBeVisible();
    expect(screen.getByRole('status')).toHaveTextContent('准备卡已生成');
    rerender(<ReproductionCard reportId={2} />);
    expect(screen.queryByText('First target')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '生成复现准备卡' })).toBeEnabled();
    await act(async () => next.resolve(success()));
    fireEvent.click(screen.getByRole('button', { name: '生成复现准备卡' }));
    expect(await screen.findByText('Second generated target')).toBeVisible();
    expect(fetch).toHaveBeenLastCalledWith(
      '/api/v1/reports/2/reproduction-card?force=false',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('clears a previous error while the next report is still loading', async () => {
    const next = deferred<Response>();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValueOnce(new Error('offline')).mockReturnValueOnce(next.promise),
    );
    const { rerender } = render(<ReproductionCard reportId={1} />);
    expect(await screen.findByRole('alert')).toBeVisible();
    rerender(<ReproductionCard reportId={2} />);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await act(async () => next.resolve(success(ready('Second target'))));
    expect(screen.getByText('Second target')).toBeVisible();
  });

  it.each(['success', 'failure'] as const)(
    'ignores a stale initial %s after switching reports',
    async (outcome) => {
      const previous = deferred<Response>();
      vi.stubGlobal(
        'fetch',
        vi
          .fn()
          .mockReturnValueOnce(previous.promise)
          .mockResolvedValueOnce(success(ready('Current target'))),
      );
      const { rerender } = render(<ReproductionCard reportId={1} />);
      rerender(<ReproductionCard reportId={2} />);
      expect(await screen.findByText('Current target')).toBeVisible();
      await act(async () => {
        if (outcome === 'success') previous.resolve(success(ready('Stale target')));
        else previous.reject(new Error('Stale error'));
      });
      expect(screen.getByText('Current target')).toBeVisible();
      expect(screen.queryByText('Stale target')).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    },
  );

  it.each(['success', 'failure'] as const)(
    'ignores a stale generation %s without ending the current report generation',
    async (outcome) => {
      const previous = deferred<Response>();
      const nextRead = deferred<Response>();
      const current = deferred<Response>();
      vi.stubGlobal(
        'fetch',
        vi
          .fn()
          .mockResolvedValueOnce(success())
          .mockReturnValueOnce(previous.promise)
          .mockReturnValueOnce(nextRead.promise)
          .mockReturnValueOnce(current.promise),
      );
      const { rerender } = render(<ReproductionCard reportId={1} />);
      expect(await screen.findByText('尚未生成')).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: '生成复现准备卡' }));
      rerender(<ReproductionCard reportId={2} />);
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: '生成复现准备卡' })).toBeEnabled();
      await act(async () => nextRead.resolve(success()));
      fireEvent.click(screen.getByRole('button', { name: '生成复现准备卡' }));
      await act(async () => {
        if (outcome === 'success') previous.resolve(success(ready('Stale target')));
        else previous.reject(new Error('Stale error'));
      });
      expect(screen.getByRole('button', { name: '正在核对原文…' })).toBeDisabled();
      expect(screen.getByRole('status')).toHaveTextContent('正在读取全文');
      expect(screen.queryByText('Stale target')).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      await act(async () => current.resolve(success(ready('Current target'))));
      expect(screen.getByText('Current target')).toBeVisible();
      expect(screen.getByRole('button', { name: '重新核对' })).toBeEnabled();
    },
  );

  it.each(['success', 'failure'] as const)(
    'ignores an initial read %s that arrives after explicit generation',
    async (outcome) => {
      const initial = deferred<Response>();
      vi.stubGlobal(
        'fetch',
        vi
          .fn()
          .mockReturnValueOnce(initial.promise)
          .mockResolvedValueOnce(success(ready('Generated target'))),
      );
      render(<ReproductionCard reportId={1} />);
      fireEvent.click(screen.getByRole('button', { name: '生成复现准备卡' }));
      expect(await screen.findByText('Generated target')).toBeVisible();
      await act(async () => {
        if (outcome === 'success') initial.resolve(success(ready('Outdated cached target')));
        else initial.reject(new Error('Stale error'));
      });
      expect(screen.getByText('Generated target')).toBeVisible();
      expect(screen.queryByText('Outdated cached target')).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(screen.getByRole('status')).toHaveTextContent('准备卡已生成');
    },
  );
});
