import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { EvaluationBadge } from './ResearchEvaluation';
import type { ResearchEvaluation } from '@/types';

function evaluation(tier: string, label: string) {
  return { tier, label } as ResearchEvaluation;
}

describe('EvaluationBadge', () => {
  it('uses the authoritative evaluation label and retains tier styling', () => {
    render(<EvaluationBadge evaluation={evaluation('focus', '优先精读')} />);
    expect(screen.getByText('优先精读')).toHaveClass('bg-emerald-50');
  });

  it('shows an unknown tier label with neutral styling', () => {
    render(<EvaluationBadge evaluation={evaluation('future-tier', '新评估状态')} />);
    expect(screen.getByText('新评估状态')).toHaveClass('bg-gray-50');
  });

  it('uses the unevaluated placeholder when no evaluation exists', () => {
    render(<EvaluationBadge />);
    expect(screen.getByText('尚未按新标准评估')).toBeVisible();
  });

  it('uses a neutral placeholder when the label is blank', () => {
    render(<EvaluationBadge evaluation={evaluation('pending', '  ')} />);
    expect(screen.getByText('待核验')).toBeVisible();
  });
});
