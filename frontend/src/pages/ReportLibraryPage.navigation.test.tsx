import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import * as api from '@/services/api';
import { deferred, report } from '@/test/fixtures';
import ReportLibraryPage from './ReportLibraryPage';

vi.mock('@/services/api', () => ({ getReports: vi.fn(), getReportStats: vi.fn() }));

beforeEach(() => {
  vi.mocked(api.getReports).mockReset();
  vi.mocked(api.getReportStats).mockResolvedValue({
    success: true,
    data: {
      total_reports: 2,
      with_full_text: 2,
      without_full_text: 0,
    },
  });
});

test('a late search response cannot overwrite the current search results', async () => {
  const first = deferred<Awaited<ReturnType<typeof api.getReports>>>();
  const second = deferred<Awaited<ReturnType<typeof api.getReports>>>();
  vi.mocked(api.getReports).mockImplementation((params) =>
    params?.search ? second.promise : first.promise,
  );
  render(
    <MemoryRouter>
      <ReportLibraryPage />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByPlaceholderText('搜索标题或摘要...'), {
    target: { value: 'new search' },
  });
  await act(async () =>
    second.resolve({
      success: true,
      data: {
        reports: [report({ id: 2, title: 'Current result' })],
        total: 1,
        limit: 20,
        offset: 0,
      },
    }),
  );
  expect(screen.getByRole('heading', { name: 'Current result' })).toBeInTheDocument();
  await act(async () =>
    first.resolve({
      success: true,
      data: { reports: [report({ id: 1, title: 'Stale result' })], total: 1, limit: 20, offset: 0 },
    }),
  );
  expect(screen.getByRole('heading', { name: 'Current result' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Stale result' })).not.toBeInTheDocument();
});
