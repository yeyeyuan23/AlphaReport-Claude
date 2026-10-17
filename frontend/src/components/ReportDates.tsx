import type { ReportSummary } from '../types';

/** Keep source announcement, publication precision and ingestion distinct. */
export function ReportDates({ report }: { report: ReportSummary }) {
  const info = report.publication_date_info;
  let publication: string | undefined;
  if (info?.precision === 'year' && info.year) {
    publication = `${info.year}（精确到年）`;
  } else if (
    info?.precision === 'month' &&
    info.year &&
    info.month &&
    info.month >= 1 &&
    info.month <= 12
  ) {
    publication = `${info.year}-${String(info.month).padStart(2, '0')}（精确到月）`;
  } else if (report.published_date && (!info?.precision || info.precision === 'day')) {
    publication = report.published_date.slice(0, 10);
  }
  const labels: string[] = [];
  if (report.announced_at) labels.push(`公告：${report.announced_at.slice(0, 10)}`);
  if (publication) labels.push(`发表：${publication}`);
  if (!labels.length) {
    labels.push('发表日期未知');
    if (report.created_at) labels.push(`入库：${report.created_at.slice(0, 10)}`);
  }
  return <>{labels.join(' · ')}</>;
}
