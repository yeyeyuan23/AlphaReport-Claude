import { act, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ResearchBriefing from './ResearchBriefing';
import type { BriefingSection } from '@/types';
import { deferred, json } from '@/test/fixtures';

function success(content: string) {
  return json({ success: true, data: { content, source: 'evaluation' } });
}

function sections(content: string): BriefingSection[] {
  return [{ title: 'Canonical findings', icon: '📌', content }];
}

describe('ResearchBriefing request states', () => {
  it('reports a failed request and retries when the panel is reopened', async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(success('Recovered briefing'));
    vi.stubGlobal('fetch', fetch);
    const { unmount, container } = render(<ResearchBriefing reportId={1} />);
    expect(await screen.findByText('offline')).toBeVisible();
    expect(container.querySelector('[data-slot="skeleton"]')).not.toBeInTheDocument();
    unmount();
    render(<ResearchBriefing reportId={1} />);
    expect(await screen.findByText('Recovered briefing')).toBeVisible();
    expect(screen.queryByText('offline')).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenLastCalledWith(
      '/api/v1/reports/1/research-briefing?force=false',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it.each([{ success: false }, { success: true, data: { sections: [], source: 'evaluation' } }])(
    'explains a missing briefing for response %j',
    async (response) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(response)));
      const { container } = render(<ResearchBriefing reportId={1} />);
      expect(await screen.findByText('暂无分析数据，请先运行 AI 分析')).toBeVisible();
      expect(container.querySelector('[data-slot="skeleton"]')).not.toBeInTheDocument();
    },
  );

  it('hides prior sections while the next report is loading and when its load fails', async () => {
    const next = deferred<Response>();
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(
          json({
            success: true,
            data: {
              source: 'evaluation',
              sections: [{ title: 'First findings', icon: '📋', content: 'First briefing' }],
            },
          }),
        )
        .mockReturnValueOnce(next.promise),
    );
    const { rerender, container } = render(<ResearchBriefing reportId={1} />);
    expect(await screen.findByText('First briefing')).toBeVisible();
    rerender(<ResearchBriefing reportId={2} />);
    expect(screen.queryByText('First briefing')).not.toBeInTheDocument();
    expect(screen.queryByText('First findings')).not.toBeInTheDocument();
    expect(container.querySelector('[data-slot="skeleton"]')).toBeInTheDocument();
    await act(async () => next.reject(new Error('Second error')));
    expect(screen.getByText('Second error')).toBeVisible();
    expect(screen.queryByText('First briefing')).not.toBeInTheDocument();
  });

  it('clears an old error while loading another report and displays the new result', async () => {
    const next = deferred<Response>();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValueOnce(new Error('First error')).mockReturnValueOnce(next.promise),
    );
    const { rerender, container } = render(<ResearchBriefing reportId={1} />);
    expect(await screen.findByText('First error')).toBeVisible();
    rerender(<ResearchBriefing reportId={2} />);
    expect(screen.queryByText('First error')).not.toBeInTheDocument();
    expect(container.querySelector('[data-slot="skeleton"]')).toBeInTheDocument();
    await act(async () => next.resolve(success('Current briefing')));
    expect(screen.getByText('Current briefing')).toBeVisible();
  });

  it.each(['success', 'failure'] as const)(
    'ignores a stale %s after switching reports',
    async (outcome) => {
      const previous = deferred<Response>();
      vi.stubGlobal(
        'fetch',
        vi
          .fn()
          .mockReturnValueOnce(previous.promise)
          .mockResolvedValueOnce(success('Current briefing')),
      );
      const { rerender } = render(<ResearchBriefing reportId={1} />);
      rerender(<ResearchBriefing reportId={2} />);
      expect(await screen.findByText('Current briefing')).toBeVisible();
      await act(async () => {
        if (outcome === 'success') previous.resolve(success('Stale briefing'));
        else previous.reject(new Error('Stale error'));
      });
      expect(screen.getByText('Current briefing')).toBeVisible();
      expect(screen.queryByText('Stale briefing')).not.toBeInTheDocument();
      expect(screen.queryByText('Stale error')).not.toBeInTheDocument();
    },
  );

  it.each(['success', 'failure'] as const)(
    'ignores an API %s after canonical sections become available for the same report',
    async (outcome) => {
      const previous = deferred<Response>();
      const fetch = vi.fn().mockReturnValueOnce(previous.promise);
      vi.stubGlobal('fetch', fetch);
      const { rerender } = render(<ResearchBriefing reportId={1} />);
      rerender(<ResearchBriefing reportId={1} briefingSections={sections('Current analysis')} />);
      expect(screen.getByText('Current analysis')).toBeVisible();
      await act(async () => {
        if (outcome === 'success') previous.resolve(success('Stale briefing'));
        else previous.reject(new Error('Stale error'));
      });
      expect(screen.getByText('Current analysis')).toBeVisible();
      expect(screen.queryByText('Stale briefing')).not.toBeInTheDocument();
      expect(screen.queryByText('Stale error')).not.toBeInTheDocument();
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  it('explains an authoritative empty briefing without another request', () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    render(<ResearchBriefing reportId={1} briefingSections={[]} />);
    expect(screen.getByText('暂无研报简报数据')).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('drops supplied sections and fetches only when the next report needs an API briefing', async () => {
    const next = deferred<Response>();
    const fetch = vi.fn().mockReturnValueOnce(next.promise);
    vi.stubGlobal('fetch', fetch);
    const { rerender } = render(
      <ResearchBriefing reportId={1} briefingSections={sections('First analysis')} />,
    );
    expect(screen.getByRole('heading', { name: '📌Canonical findings' })).toBeVisible();
    expect(screen.getByText('First analysis')).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
    rerender(<ResearchBriefing reportId={2} />);
    expect(screen.queryByText('First analysis')).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/reports/2/research-briefing?force=false',
      expect.objectContaining({ method: 'POST' }),
    );
    await act(async () => next.resolve(success('Second briefing')));
    expect(screen.getByText('Second briefing')).toBeVisible();
  });
});
