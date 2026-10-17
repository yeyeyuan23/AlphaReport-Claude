import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import * as api from '@/services/api';
import { deferred, report } from '@/test/fixtures';
import ReportDetailPage from './ReportDetailPage';

vi.mock('@/services/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  getReport: vi.fn(),
  getKnowledgeCard: vi.fn(),
  getReproductionCard: vi.fn(),
  getResearchBriefing: vi.fn(),
}));

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('missing', { status: 404 })));
  vi.mocked(api.getReport).mockReset();
  vi.mocked(api.getKnowledgeCard).mockResolvedValue({
    success: true,
    data: {
      summary: '',
      highlights: [],
      methodology_steps: [],
      results: '',
      marginal_contributions: '',
      implications: [],
      topics: [],
      asset_classes: [],
      quality_score: null,
      status: 'pending',
      quality_label: '',
      notice: 'Awaiting analysis',
      limitations: [],
      evidence: [],
    },
  });
});

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/library/1']}>
      <Link to="/library/2">Open second report</Link>
      <Routes>
        <Route path="/library/:id" element={<ReportDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

test('a late report response cannot replace the newly selected report', async () => {
  const first = deferred<Awaited<ReturnType<typeof api.getReport>>>();
  const second = deferred<Awaited<ReturnType<typeof api.getReport>>>();
  vi.mocked(api.getReport).mockImplementation((id) => (id === 1 ? first.promise : second.promise));
  renderDetail();
  fireEvent.click(screen.getByText('Open second report'));
  await act(async () =>
    second.resolve({ success: true, data: report({ id: 2, title: 'Second report' }) }),
  );
  expect(screen.getByRole('heading', { name: 'Second report' })).toBeInTheDocument();
  await act(async () =>
    first.resolve({ success: true, data: report({ id: 1, title: 'First report' }) }),
  );
  expect(screen.getByRole('heading', { name: 'Second report' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'First report' })).not.toBeInTheDocument();
});

test('an unavailable newly selected report clears the previous report', async () => {
  vi.mocked(api.getReport).mockImplementation(async (id) => ({
    success: id === 1,
    data: report({ id, title: `Report ${id}` }),
  }));
  renderDetail();
  await screen.findByRole('heading', { name: 'Report 1' });
  fireEvent.click(screen.getByText('Open second report'));
  expect(await screen.findByText('研报不存在')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Report 1' })).not.toBeInTheDocument();
});
