export type SourceType = "local_pdf" | "arxiv" | "ssrn" | "eastmoney" | "bigquant" | "broker" | "other_web";

export type Market = "china" | "overseas" | "global";
export type AssetClass = "stock" | "futures" | "options" | "fixed_income" | "crypto" | "multi_asset";
export type Frequency = "hft" | "mid_freq" | "low_freq" | "mixed";
export type Topic =
  | "risk_model" | "factor_model" | "ai_ml_model" | "execution_algo"
  | "portfolio_optimization" | "market_microstructure" | "alternative_data"
  | "volatility" | "statistical_arbitrage" | "other";

export interface UserCriteria {
  topics: string[];
  sources: SourceType[];
  keywords: string[];
  date_from?: string;
  date_to?: string;
  max_results_per_source: number;
  journals: string[];
  brokers: string[];
  local_pdf_path?: string;
}

export interface ReportSummary {
  id: number;
  title: string;
  authors: string[];
  abstract?: string;
  source: SourceType;
  source_url?: string;
  doi?: string;
  arxiv_id?: string;
  published_date?: string;
  has_full_text: boolean;
  pdf_path?: string;
  markets: string[];
  asset_classes: string[];
  frequencies: string[];
  topics: string[];
  custom_tags: string[];
  content_hash: string;
  selection_relevance?: number | null;
  selection_direction?: string | null;
  selection_reason?: string | null;
  evaluation?: ResearchEvaluation | null;
  created_at: string;
  updated_at: string;
}

export interface ReportDetail extends ReportSummary {
  full_text?: string;
  summary?: string;
  tables_json?: string;
  equations_json?: string;
  analysis?: AnalysisResult;
}

export interface ResearchEvidence {
  claim: string;
  quote: string;
  chunk_id: string;
  verified: true;
  locator: string;
}

export interface ResearchEvaluation {
  tier: "focus" | "browse" | "pending" | "exclude";
  label: string;
  status: "reviewed" | "needs_verification" | "pending";
  source_category: "academic" | "broker" | "community" | "other";
  relevance: number | null;
  relevance_reason: string;
  research_type?: "empirical" | "theoretical" | "methodological" | "broker" | "community";
  research_question: string;
  method: ResearchEvidence | null;
  findings: ResearchEvidence[];
  strengths: ResearchEvidence[];
  limitations: string[];
  critical_concerns: string[];
  quality: "strong" | "sound" | "weak" | "unknown";
  confidence: "high" | "medium" | "low";
  text_complete: boolean;
  validation_issues: string[];
  reading_advice: string;
  reviewed_at: string;
  model?: string;
}

export interface ReportTable {
  table_body: string;
  table_caption?: string[];
  page_idx?: number;
}

export interface ReportEquation {
  latex: string;
  text?: string;
  page_idx?: number;
  meaning?: string;
  symbols?: Record<string, string>;
  role_in_paper?: string;
  is_key_formula?: boolean;
}

// ── Analysis ──

export interface DataInfo {
  market: string;
  instruments: string[];
  frequency: string;
  sample_period: string;
  universe: string;
}

export interface FactorInfo {
  name: string;
  type: string;
  construction: string;
  raw_or_neutralized: string;
  formula_index?: number;
}

export interface Methodology {
  analysis_points: Record<string, unknown>[];
  factor_list: FactorInfo[];
  model_architecture?: {
    type: string;
    layers_or_structure: string;
    loss_function: string;
    regularization: string;
    training_scheme: string;
  };
  portfolio_construction?: {
    weighting: string;
    rebalance_frequency: string;
    constraints: string;
    transaction_cost_model: string;
  };
}

export interface EquationExplanation {
  index: number;
  latex: string;
  meaning: string;
  symbols: Record<string, string>;
  role_in_paper: string;
  is_key_formula: boolean;
}

export interface BiasRisks {
  look_ahead_bias: string;
  survivorship_bias: string;
  data_snooping: string;
  overfitting_concern: string;
}

export interface Assessment {
  overall_quality_score: number;
  strengths: string[];
  weaknesses: string[];
  bias_risks: BiasRisks;
  reproducibility: {
    level: string;
    has_data_source: boolean;
    has_code_available: boolean;
    missing_details: string[];
  };
  a_share_applicability?: {
    directly_applicable: boolean;
    adaptations_needed: string[];
    key_constraints: string[];
  };
  marginal_contribution_summary: string;
  practical_implications: string[];
  key_contributions: string[];
}

export interface AnalysisResult {
  research_question: string;
  core_contribution: string;
  method_category: string;
  data_used: DataInfo;
  benchmark_models: string[];
  methodology?: Methodology;
  equations: EquationExplanation[];
  assessment?: Assessment;
  summary: string;
  depth: string;
  analyzed_at: string;
}

export interface KnowledgeCard {
  summary: string;
  highlights: string[];
  methodology_steps: string[];
  results: string;
  marginal_contributions: string;
  implications: string[];
  topics: string[];
  asset_classes: string[];
  quality_score: number | null;
  status: "ready" | "pending";
  quality_label: string;
  notice: string;
  limitations: string[];
  evidence: { claim: string; quote: string; raw_quote: string; locator: string }[];
}

export interface ReproductionCard {
  status: "ready" | "pending" | "retryable_error";
  notice: string;
  fields?: Record<string, {
    status: "documented" | "needs_verification" | "not_applicable";
    value: string;
    evidence: { quote: string; raw_quote: string; locator: string }[];
  }>;
  minimum_check?: string;
  falsification?: string;
  proposal_label?: string;
  open_questions?: string[];
}

export const BIAS_RISK_LABELS: Record<string, string> = {
  none: "无风险",
  low: "低风险",
  medium: "中风险",
  high: "高风险",
  unknown: "未知",
};

export const METHOD_CATEGORY_LABELS: Record<string, string> = {
  factor_model: "因子模型",
  ai_ml_model: "AI/机器学习",
  statistical_method: "统计方法",
  theoretical: "理论推导",
  empirical: "实证研究",
  other: "其他",
};

export interface CollectionTask {
  task_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  phase: "collecting" | "classifying" | "storing" | "complete";
  progress_message: string;
  results_count: number;
  storage_result?: {
    total_processed: number;
    newly_added: number;
    updated: number;
    duplicate_skipped: number;
  };
  created_at: string;
  updated_at: string;
}

export interface ReportStats {
  total_reports: number;
  with_full_text: number;
  without_full_text: number;
}

export interface ApiResponse<T = unknown> {
  success: boolean;
  data?: T;
  error?: string;
  message?: string;
}

export const SOURCE_LABELS: Record<SourceType, string> = {
  local_pdf: "本地PDF",
  arxiv: "arXiv",
  ssrn: "SSRN",
  eastmoney: "东方财富",
  bigquant: "BigQuant",
  broker: "券商",
  other_web: "其他网络",
};

export const MARKET_LABELS: Record<Market, string> = {
  china: "中国市场",
  overseas: "海外市场",
  global: "全球市场",
};

export const ASSET_LABELS: Record<AssetClass, string> = {
  stock: "股票",
  futures: "期货/CTA",
  options: "期权/衍生品",
  fixed_income: "固收/债券",
  crypto: "加密货币",
  multi_asset: "多资产",
};

export const FREQUENCY_LABELS: Record<Frequency, string> = {
  hft: "高频",
  mid_freq: "中频",
  low_freq: "低频/日频+",
  mixed: "混合",
};

// ── Chat ──

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  toolCalls?: ToolCallEvent[];
  timestamp: number;
  isStreaming?: boolean;
}

export interface ToolCallEvent {
  name: string;
  args: Record<string, unknown>;
  result?: string;
}

export interface ChatConversation {
  id: string;
  user_id?: string;
  email?: string;
  title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export const TOPIC_LABELS: Record<Topic, string> = {
  risk_model: "风险模型",
  factor_model: "因子模型/Alpha",
  ai_ml_model: "AI/机器学习",
  execution_algo: "执行算法",
  portfolio_optimization: "组合优化",
  market_microstructure: "微观结构",
  alternative_data: "另类数据",
  volatility: "波动率",
  statistical_arbitrage: "统计套利",
  other: "其他",
};
