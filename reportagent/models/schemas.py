from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Market(str, Enum):
    CHINA = "china"
    OVERSEAS = "overseas"
    GLOBAL = "global"


class AssetClass(str, Enum):
    STOCK = "stock"
    FUTURES = "futures"
    OPTIONS = "options"
    FIXED_INCOME = "fixed_income"
    CRYPTO = "crypto"
    MULTI_ASSET = "multi_asset"


class Frequency(str, Enum):
    HFT = "hft"
    MID_FREQ = "mid_freq"
    LOW_FREQ = "low_freq"
    MIXED = "mixed"


class Topic(str, Enum):
    RISK_MODEL = "risk_model"
    FACTOR_MODEL = "factor_model"
    AI_ML_MODEL = "ai_ml_model"
    EXECUTION_ALGO = "execution_algo"
    PORTFOLIO_OPT = "portfolio_optimization"
    MARKET_MICRO = "market_microstructure"
    ALTERNATIVE_DATA = "alternative_data"
    VOLATILITY = "volatility"
    STATISTICAL_ARB = "statistical_arbitrage"
    OTHER = "other"


class SourceType(str, Enum):
    LOCAL_PDF = "local_pdf"
    ARXIV = "arxiv"
    SSRN = "ssrn"
    EASTMONEY = "eastmoney"
    BIGQUANT = "bigquant"
    BROKER = "broker"
    OTHER_WEB = "other_web"


class UserCriteria(BaseModel):
    topics: list[str] = Field(..., description="Search topics")
    sources: list[SourceType] = Field(
        default_factory=lambda: [
            SourceType.LOCAL_PDF,
            SourceType.ARXIV,
            SourceType.EASTMONEY,
            SourceType.BIGQUANT,
        ],
    )
    keywords: list[str] = Field(default_factory=list)
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    max_results_per_source: int | None = Field(default=20, ge=1, le=100)
    date_basis: Literal["publication", "announcement"] = "publication"
    journals: list[str] = Field(default_factory=list)
    brokers: list[str] = Field(default_factory=list)
    local_pdf_path: Optional[str] = None

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def calendar_date(cls, value):
        from reportagent.sources.date_window import source_date

        if value is None:
            return None
        day = source_date(value)
        if day is None:
            raise ValueError("Expected a source calendar date (YYYY-MM-DD)")
        return day


class SearchResult(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    abstract: Optional[str] = None
    full_text: Optional[str] = None
    abstract_only: bool = False
    source: SourceType
    source_url: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    published_date: Optional[date] = None
    pdf_path: Optional[str] = None
    raw_metadata: dict = Field(default_factory=dict)
    tables_json: Optional[str] = None
    equations_json: Optional[str] = None

    @field_validator("published_date", mode="before")
    @classmethod
    def calendar_date(cls, value):
        from reportagent.sources.date_window import source_date

        if value is None:
            return None
        day = source_date(value)
        if day is None:
            raise ValueError("Expected a source calendar date (YYYY-MM-DD)")
        return day


class ClassificationResult(BaseModel):
    markets: list[Market] = Field(default_factory=list)
    asset_classes: list[AssetClass] = Field(default_factory=list)
    frequencies: list[Frequency] = Field(default_factory=list)
    topics: list[Topic] = Field(default_factory=list)
    custom_tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    method: str = "rule"
    provenance: dict = Field(default_factory=dict)


class ClassifiedReport(BaseModel):
    search_result: SearchResult
    classification: ClassificationResult
    analysis: Optional[AnalysisResult] = None


class StorageResult(BaseModel):
    total_processed: int = 0
    newly_added: int = 0
    updated: int = 0
    duplicate_skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    report_ids: list[int] = Field(default_factory=list)


class DataInfo(BaseModel):
    market: str = ""
    instruments: list[str] = Field(default_factory=list)
    frequency: str = ""
    sample_period: str = ""
    universe: str = ""


class FactorInfo(BaseModel):
    name: str = ""
    type: str = ""
    construction: str = ""
    raw_or_neutralized: str = ""
    formula_index: Optional[int] = None


class ModelArchitecture(BaseModel):
    type: str = ""
    layers_or_structure: str = ""
    loss_function: str = ""
    regularization: str = ""
    training_scheme: str = ""


class PortfolioConstruction(BaseModel):
    weighting: str = ""
    rebalance_frequency: str = ""
    constraints: str = ""
    transaction_cost_model: str = ""


class Methodology(BaseModel):
    analysis_points: list[dict] = Field(default_factory=list)
    factor_list: list[FactorInfo] = Field(default_factory=list)
    model_architecture: Optional[ModelArchitecture] = None
    portfolio_construction: Optional[PortfolioConstruction] = None


class EquationExplanation(BaseModel):
    index: int = 0
    latex: str = ""
    meaning: str = ""
    symbols: dict[str, str] = Field(default_factory=dict)
    role_in_paper: str = ""
    is_key_formula: bool = False


class BiasRisks(BaseModel):
    look_ahead_bias: str = "unknown"
    survivorship_bias: str = "unknown"
    data_snooping: str = "unknown"
    overfitting_concern: str = ""


class QualityCheck(BaseModel):
    dimension: Literal["method", "validation", "scope", "reproducibility"]
    status: Literal["supported", "limited", "unclear", "not_applicable"]
    basis: str = Field(min_length=1)

    @field_validator("basis")
    @classmethod
    def nonempty_basis(cls, value):
        if not value.strip():
            raise ValueError("Quality checks require an evidence basis")
        return value.strip()


class QualityJudgment(BaseModel):
    quality: Literal["strong", "sound", "weak", "unknown"]
    confidence: Literal["high", "medium", "low"]
    quality_checks: list[QualityCheck] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def consistent_judgment(self):
        checks = {check.dimension: check.status for check in self.quality_checks}
        if set(checks) != {"method", "validation", "scope", "reproducibility"}:
            raise ValueError("Quality checks must cover each dimension exactly once")
        core = [checks[key] for key in ("method", "validation", "scope")]
        if self.quality == "strong" and any(s != "supported" for s in core):
            raise ValueError("Strong quality requires supported core evidence")
        if self.confidence == "high" and (
            self.quality == "unknown" or "unclear" in core
        ):
            raise ValueError("High confidence requires assessable core evidence")
        return self


class Reproducibility(BaseModel):
    level: str = "unknown"
    has_data_source: bool | None = None
    has_code_available: bool | None = None
    missing_details: list[str] = Field(default_factory=list)


class AShareApplicability(BaseModel):
    directly_applicable: bool | None = None
    adaptations_needed: list[str] = Field(default_factory=list)
    key_constraints: list[str] = Field(default_factory=list)


class Assessment(BaseModel):
    # Defaults allow reading legacy artifacts; new model responses use QualityJudgment.
    quality: Literal["strong", "sound", "weak", "unknown"] = "unknown"
    confidence: Literal["high", "medium", "low"] = "low"
    quality_checks: list[QualityCheck] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    bias_risks: BiasRisks = Field(default_factory=BiasRisks)
    reproducibility: Reproducibility = Field(default_factory=Reproducibility)
    a_share_applicability: Optional[AShareApplicability] = None
    marginal_contribution_summary: str = ""
    practical_implications: list[str] = Field(default_factory=list)
    key_contributions: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    research_question: str = ""
    core_contribution: str = ""
    method_category: str = ""
    data_used: DataInfo = Field(default_factory=DataInfo)
    benchmark_models: list[str] = Field(default_factory=list)
    methodology: Optional[Methodology] = None
    equations: list[EquationExplanation] = Field(default_factory=list)
    assessment: Optional[Assessment] = None
    summary: str = ""
    depth: str = "standard"
    analyzed_at: str = ""


class ReportListParams(BaseModel):
    market: Optional[str] = None
    asset_class: Optional[str] = None
    frequency: Optional[str] = None
    topic: Optional[str] = None
    search: Optional[str] = None
    source: Optional[str] = None
    has_full_text: Optional[bool] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    sort_by: str = "created_at"
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
