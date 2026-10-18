import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import ResearchEvaluation, { EvaluationBadge } from './ResearchEvaluation';
import type { ResearchEvaluation as Evaluation } from '@/types';

function evaluation(): Evaluation {
  return {
    tier: 'focus',
    label: '重点阅读',
    status: 'reviewed',
    source_category: 'broker',
    relevance: 8,
    relevance_reason: '组合优化',
    research_type: 'theoretical',
    research_question: '何时成立？',
    method: null,
    findings: [],
    strengths: [],
    limitations: [],
    critical_concerns: [],
    quality: 'strong',
    confidence: 'high',
    text_complete: true,
    validation_issues: [],
    reading_advice: '核对假设',
    reviewed_at: '',
    quality_checks: [
      { dimension: 'method', status: 'supported', basis: '方法与假设有明确原文依据。' },
      {
        dimension: 'validation',
        status: 'supported',
        basis: '命题证明支持条件结论，无需交易回测。',
      },
      { dimension: 'scope', status: 'supported', basis: '结论仅适用于连续价格路径。' },
      { dimension: 'reproducibility', status: 'limited', basis: '未开源实现，不作为低质量依据。' },
    ],
  };
}

describe('Shared research quality evidence', () => {
  it('shows research design independently of document type and the four evidence checks', () => {
    render(<ResearchEvaluation evaluation={evaluation()} />);
    expect(screen.getByText('券商研报')).toBeVisible();
    expect(screen.getByText('理论研究')).toBeVisible();
    expect(screen.getByText('研究质量：较强')).toBeVisible();
    for (const check of evaluation().quality_checks!)
      expect(screen.getByText(check.basis)).toBeVisible();
  });

  it('can still display a pending or legacy record without dimension checks', () => {
    const record = evaluation();
    delete record.quality_checks;
    render(<ResearchEvaluation evaluation={record} />);
    expect(screen.queryByText('质量判断依据')).not.toBeInTheDocument();
    expect(screen.getByText('核对假设')).toBeVisible();
  });
});

function badgeEvaluation(tier: string, label: string) {
  return { tier, label } as Evaluation;
}

describe('EvaluationBadge', () => {
  it('uses the authoritative evaluation label and retains tier styling', () => {
    render(<EvaluationBadge evaluation={badgeEvaluation('focus', '优先精读')} />);
    expect(screen.getByText('优先精读')).toHaveClass('bg-emerald-50');
  });

  it('shows an unknown tier label with neutral styling', () => {
    render(<EvaluationBadge evaluation={badgeEvaluation('future-tier', '新评估状态')} />);
    expect(screen.getByText('新评估状态')).toHaveClass('bg-gray-50');
  });

  it('uses the unevaluated placeholder when no evaluation exists', () => {
    render(<EvaluationBadge />);
    expect(screen.getByText('尚未按新标准评估')).toBeVisible();
  });

  it('uses a neutral placeholder when the label is blank', () => {
    render(<EvaluationBadge evaluation={badgeEvaluation('pending', '  ')} />);
    expect(screen.getByText('待核验')).toBeVisible();
  });
});
