"""Mission Control public-API types — Python edition.

Optional. Use this if risedual.ai's backend re-shapes / re-validates MC
responses before passing them through to the frontend (defensive
deserialization). If you just proxy responses verbatim, you don't need
this file.

Pydantic v2. Drop into your backend, e.g. `backend/services/mc_types.py`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


Tier = Literal["free", "starter", "pro", "pro_max"]
ConsensusLabel = Literal["BULLISH", "BEARISH", "NEUTRAL", "MIXED"]
ColorBand = Literal[
    "strong_buy", "mild_buy", "neutral", "mild_sell", "strong_sell",
]


# ──────────────────────── shared ────────────────────────

class PublicCaller(BaseModel):
    tier: Tier
    is_paid: bool
    is_unlimited: bool


class ConsensusBreakdown(BaseModel):
    buy_pct: int
    sell_pct: int
    hold_pct: int
    n: Optional[int] = None


# ──────────────────────── /signals ────────────────────────

class SignalCard(BaseModel):
    signal_id: str
    symbol: str
    direction: Literal["LONG", "SHORT", "HOLD"]
    state: str
    flagged_by_auditor: bool
    consensus: ConsensusLabel
    consensus_breakdown: ConsensusBreakdown
    thesis: str
    updated_at: str
    created_at: str


class ConsensusHero(ConsensusBreakdown):
    label: ConsensusLabel


class SignalsResponse(BaseModel):
    items: list[SignalCard]
    count: int
    active_signals: int
    consensus: ConsensusHero
    caller: PublicCaller


class AgentSpeakBlock(BaseModel):
    label: str
    stance: Literal["LONG", "SHORT", "ABSTAIN"]
    confidence: int
    notes: str


class AdversarialView(BaseModel):
    bull: Optional[AgentSpeakBlock]
    bear: Optional[AgentSpeakBlock]
    commander: Optional[AgentSpeakBlock]


class StrategistBlock(BaseModel):
    label: Literal["STRATEGIST_AGENT"]
    proposal: str
    confidence: int
    detected: str


class AuditorBlock(BaseModel):
    label: Literal["RISK_AUDITOR_AGENT"]
    action: Literal["PASS", "VETO"]
    mode: str
    confidence: int


class SynthesizedBlock(BaseModel):
    label: Literal["SYNTHESIZED SIGNAL"]
    symbol: str
    direction: Literal["LONG", "SHORT", "HOLD"]
    confidence: int


class GovernanceView(BaseModel):
    strategist: StrategistBlock
    auditor: AuditorBlock
    synthesized: SynthesizedBlock


class SignalDetail(SignalCard):
    adversarial: AdversarialView
    governance: GovernanceView
    caller: PublicCaller


# ──────────────────────── /digest ────────────────────────

class DigestPrediction(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT", "HOLD", "NO_TRADE"]
    confidence: int
    price: Optional[float] = None


class DigestSmartMoney(BaseModel):
    symbol: str
    score: int
    signal: Literal["bullish", "bearish", "neutral"]
    net_flow_usd: Optional[float] = None
    bullish: int
    bearish: int


class DigestAlert(BaseModel):
    symbol: str
    delta: int
    signal_change: str


class LockedRow(BaseModel):
    symbol: None = None
    locked: Literal[True]
    kind: Literal["predictions", "smart_money", "alerts"]
    upgrade_to: Tier
    cta: str


class DigestCaps(BaseModel):
    predictions: int
    smart_money: int
    alerts: int


class DigestOverview(BaseModel):
    active_signals: int
    summary: str


class DigestResponse(BaseModel):
    # Each list mixes regular rows + LockedRow for free/starter tiers.
    # If you re-validate, accept either type; if you proxy verbatim,
    # forward as-is.
    predictions: list[dict]
    smart_money: list[dict]
    alerts: list[dict]
    overview: DigestOverview
    watchlist: None = None
    caps: DigestCaps
    tier: Tier


# ──────────────────────── /scanner ────────────────────────

ScannerPresetId = Literal[
    "macd_bullish_cross", "macd_bearish_cross", "bollinger_squeeze",
    "ema_golden_cross", "volume_spike", "near_52w_high",
    "near_52w_low", "rsi_overbought", "rsi_oversold",
    "momentum_breakout",
]


class ScannerPreset(BaseModel):
    preset_id: ScannerPresetId
    name: str
    category: str
    signal: Literal["bullish", "bearish", "neutral"]


class ScannerMatch(BaseModel):
    symbol: str
    strength: int
    detail: str


class ScannerPresetsResponse(BaseModel):
    presets: list[ScannerPreset]
    count: int
    tier: Tier


class ScannerScanResponse(BaseModel):
    preset_id: ScannerPresetId
    name: str
    category: str
    signal: Literal["bullish", "bearish", "neutral"]
    matches: list[ScannerMatch]
    scanned: int
    matched: int


# ──────────────────────── /agent-activity ────────────────────────

AgentActivityType = Literal[
    "signal_proposed", "stance_posted", "paper_trade_open",
    "paper_trade_skip", "prediction_resolved", "info",
]
AgentActivitySeverity = Literal["info", "success", "warn", "error"]


class AgentActivityEvent(BaseModel):
    event_id: str
    timestamp: str
    type: AgentActivityType
    severity: AgentActivitySeverity
    title: str
    detail: Optional[str] = None
    symbol: Optional[str] = None
    metadata: dict = {}


class AgentActivityResponse(BaseModel):
    items: list[AgentActivityEvent]
    count: int
    since: Optional[str] = None
    polled_at: str
    tier: Tier


# ──────────────────────── /models-mind ────────────────────────

class FeatureScalar(BaseModel):
    score: Optional[float] = None
    value: Optional[float | str] = None
    coverage: Optional[Literal["live", "not_wired"]] = None
    units: Optional[str] = None
    label: Optional[str] = None
    direction: Optional[Literal["up", "down"]] = None


class ModelsMindFeatures(BaseModel):
    score_2W: Optional[FeatureScalar] = None
    distance_from_mw: Optional[FeatureScalar] = None
    macro_regime_flag: Optional[FeatureScalar] = None
    atr_id: Optional[FeatureScalar] = None
    earnings_proximity: Optional[FeatureScalar] = None
    momentum_3d: Optional[FeatureScalar] = None
    sector_rs: Optional[FeatureScalar] = None
    pattern_score: Optional[FeatureScalar] = None
    rsi_id: Optional[FeatureScalar] = None
    vol_zscore: Optional[FeatureScalar] = None


class ModelsMindResponse(BaseModel):
    symbol: str
    source: str
    tf: str
    last_close: Optional[float] = None
    last_bar_ts: Optional[str] = None
    features: ModelsMindFeatures
    computed_at: str
    tier: Tier


# ──────────────────────── /heatmap + /sectors ────────────────────────

class HeatmapItem(BaseModel):
    symbol: str
    change_24h_pct: float
    color_band: ColorBand
    source: str


class HeatmapResponse(BaseModel):
    items: list[HeatmapItem]
    count: int
    degraded: bool
    tier: Tier


class SectorItem(BaseModel):
    symbol: str
    name: str
    change_24h_pct: Optional[float] = None
    color_band: ColorBand
    coverage: Literal["live", "not_wired"]


class SectorsResponse(BaseModel):
    items: list[SectorItem]
    best: Optional[SectorItem] = None
    worst: Optional[SectorItem] = None
    degraded: bool
    tier: Tier
