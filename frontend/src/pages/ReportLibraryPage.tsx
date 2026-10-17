import { useState, useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { getReports, getReportStats } from '../services/api';
import { EvaluationBadge, RelevanceLabel } from '../components/ResearchEvaluation';
import type { ReportSummary, ReportStats } from '../types';
import {
  SOURCE_LABELS,
  MARKET_LABELS,
  ASSET_LABELS,
  FREQUENCY_LABELS,
  TOPIC_LABELS,
} from '../types';

const FILTERS = [
  { key: 'market', label: '市场', options: MARKET_LABELS },
  { key: 'asset_class', label: '资产', options: ASSET_LABELS },
  { key: 'frequency', label: '频率', options: FREQUENCY_LABELS },
  { key: 'topic', label: '主题', options: TOPIC_LABELS },
] as const;

export default function ReportLibraryPage() {
  const [stats, setStats] = useState<ReportStats | null>(null);
  const [search, setSearch] = useState('');
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [offset, setOffset] = useState(0);
  const limit = 20;

  const params = useMemo(
    () => ({ search: search || undefined, ...filters, limit, offset }),
    [search, filters, offset],
  );
  const [result, setResult] = useState<{
    params: typeof params;
    reports: ReportSummary[];
    total: number;
  } | null>(null);
  const loading = result?.params !== params;
  const reports = result?.reports ?? [];
  const total = result?.total ?? 0;

  useEffect(() => {
    let active = true;
    getReports(params)
      .then((res) => {
        if (!active) return;
        setResult({
          params,
          reports: res.success && res.data ? res.data.reports : [],
          total: res.success && res.data ? res.data.total : 0,
        });
      })
      .catch(() => {
        if (active) setResult({ params, reports: [], total: 0 });
      });
    return () => {
      active = false;
    };
  }, [params]);

  useEffect(() => {
    let active = true;
    getReportStats()
      .then((res) => {
        if (active && res.success && res.data) setStats(res.data);
      })
      .catch(() => {
        /* Statistics are optional; keep the library usable on failure. */
      });
    return () => {
      active = false;
    };
  }, []);

  const setFilter = (key: string, val: string) => {
    setOffset(0);
    setFilters((prev) => {
      if (prev[key] === val) {
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return { ...prev, [key]: val };
    });
  };

  return (
    <div className="flex gap-6">
      {/* Sidebar */}
      <aside className="w-56 shrink-0 hidden lg:block">
        {stats && (
          <div className="bg-white rounded-xl border border-gray-200 p-4 mb-4">
            <div className="text-2xl font-bold text-gray-900">{stats.total_reports}</div>
            <div className="text-xs text-gray-500">总研报数</div>
            <div className="mt-2 flex gap-3 text-xs text-gray-500">
              <span>{stats.with_full_text} 全文</span>
              <span>{stats.without_full_text} 摘要</span>
            </div>
          </div>
        )}

        {FILTERS.map(({ key, label, options }) => (
          <div key={key} className="bg-white rounded-xl border border-gray-200 p-4 mb-3">
            <h3 className="text-xs font-semibold text-gray-500 uppercase mb-2">{label}</h3>
            <div className="space-y-1">
              {Object.entries(options).map(([val, lbl]) => (
                <button
                  key={val}
                  onClick={() => setFilter(key, val)}
                  className={`block w-full text-left px-2 py-1.5 rounded text-sm transition-colors ${
                    filters[key] === val
                      ? 'bg-indigo-50 text-indigo-700 font-medium'
                      : 'text-gray-600 hover:bg-gray-50'
                  }`}
                >
                  {lbl}
                </button>
              ))}
            </div>
          </div>
        ))}
      </aside>

      {/* Main */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-3 mb-4">
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setOffset(0);
            }}
            placeholder="搜索标题或摘要..."
            className="flex-1 px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-200"
          />
          {Object.keys(filters).length > 0 && (
            <button
              onClick={() => setFilters({})}
              className="text-xs text-gray-500 hover:text-gray-700"
            >
              清除筛选
            </button>
          )}
        </div>

        {/* Active filter chips */}
        {Object.keys(filters).length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-3">
            {Object.entries(filters).map(([k, v]) => {
              const dim = FILTERS.find((f) => f.key === k);
              const label = dim ? (dim.options as Record<string, string>)[v] || v : v;
              return (
                <span
                  key={k}
                  className="inline-flex items-center gap-1 px-2.5 py-1 bg-indigo-50 text-indigo-700 rounded-full text-xs"
                >
                  {label}
                  <button onClick={() => setFilter(k, v)}>&times;</button>
                </span>
              );
            })}
          </div>
        )}

        {loading ? (
          <div className="text-center py-12 text-gray-400">加载中...</div>
        ) : reports.length === 0 ? (
          <div className="text-center py-12">
            <p className="text-gray-400">暂无研报</p>
            <p className="text-xs text-gray-300 mt-1">请通过飞书机器人采集研报后刷新此页</p>
          </div>
        ) : (
          <>
            <div className="space-y-3">
              {reports.map((r) => (
                <ReportCard key={r.id} report={r} />
              ))}
            </div>

            {total > limit && (
              <div className="flex items-center justify-between mt-4 text-sm">
                <span className="text-gray-500">
                  {offset + 1}-{Math.min(offset + limit, total)} / {total}
                </span>
                <div className="flex gap-2">
                  <button
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - limit))}
                    className="px-3 py-1.5 border border-gray-200 rounded-lg disabled:opacity-40"
                  >
                    上一页
                  </button>
                  <button
                    disabled={offset + limit >= total}
                    onClick={() => setOffset(offset + limit)}
                    className="px-3 py-1.5 border border-gray-200 rounded-lg disabled:opacity-40"
                  >
                    下一页
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function ReportCard({ report }: { report: ReportSummary }) {
  const dimTags = [
    ...report.topics.map((t) => TOPIC_LABELS[t as keyof typeof TOPIC_LABELS] || t),
  ].filter(Boolean);

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 transition-all hover:border-indigo-200 hover:shadow-sm">
      <div className="flex items-start gap-3">
        {/* Content - clickable to detail */}
        <Link to={`/library/${report.id}`} className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <h3 className="font-medium text-gray-900 text-sm leading-snug line-clamp-2">
                {report.title}
              </h3>
              {report.authors.length > 0 && (
                <p className="text-xs text-gray-400 mt-1">{report.authors.join(', ')}</p>
              )}
            </div>
            <div className="shrink-0 flex flex-col items-end gap-1">
              <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">
                {SOURCE_LABELS[report.source] || report.source}
              </span>
              {report.has_full_text && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-green-50 text-green-600">
                  全文
                </span>
              )}
            </div>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <EvaluationBadge evaluation={report.evaluation} />
            <RelevanceLabel value={report.selection_relevance ?? report.evaluation?.relevance} />
          </div>

          {report.evaluation?.research_question && (
            <p className="text-xs text-gray-700 mt-2 line-clamp-2 leading-relaxed">
              <span className="font-medium">研究问题：</span>
              {report.evaluation.research_question}
            </p>
          )}
          {report.evaluation?.findings?.length ? (
            <p className="text-xs text-gray-500 mt-1 line-clamp-2 leading-relaxed">
              <span className="font-medium">主要发现：</span>
              {report.evaluation.findings[0].claim}
            </p>
          ) : (
            report.abstract && (
              <p className="text-xs text-gray-500 mt-2 line-clamp-2 whitespace-pre-line">
                {report.abstract}
              </p>
            )
          )}

          {dimTags.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {dimTags.slice(0, 4).map((t) => (
                <span
                  key={t}
                  className="px-2 py-0.5 bg-indigo-50 text-indigo-600 rounded text-[10px]"
                >
                  {t}
                </span>
              ))}
              {dimTags.length > 4 && (
                <span className="px-2 py-0.5 bg-gray-100 text-gray-400 rounded text-[10px]">
                  +{dimTags.length - 4}
                </span>
              )}
            </div>
          )}

          <div className="text-[10px] text-gray-300 mt-2">
            {report.published_date?.slice(0, 10) || report.created_at?.slice(0, 10)}
          </div>
        </Link>
      </div>
    </div>
  );
}
