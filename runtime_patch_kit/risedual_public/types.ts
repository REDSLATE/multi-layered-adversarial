/**
 * Mission Control public API — TypeScript types.
 *
 * Drop into your frontend's types folder. These mirror exactly what
 * MC's `/api/public/*` endpoints return. If MC ever changes a shape,
 * we bump the version comment at the top of each section and you
 * regenerate your client.
 *
 * Doctrine summary:
 *   - You never call MC from the browser. Proxy through your backend.
 *   - X-RiseDual-Token is server-side only.
 *   - X-RiseDual-User-Tier is propagated per-request from your user model.
 */

// ──────────────────────── shared ────────────────────────

export type Tier = "free" | "starter" | "pro" | "pro_max";

export interface PublicCaller {
  tier: Tier;
  is_paid: boolean;
  is_unlimited: boolean;
}

export type ConsensusLabel = "BULLISH" | "BEARISH" | "NEUTRAL" | "MIXED";

export interface ConsensusBreakdown {
  buy_pct: number;
  sell_pct: number;
  hold_pct: number;
  n?: number;
}

// ──────────────────────── /signals ────────────────────────

export interface SignalCard {
  signal_id: string;
  symbol: string;
  direction: "LONG" | "SHORT" | "HOLD";
  state: string;
  flagged_by_auditor: boolean;
  consensus: ConsensusLabel;
  consensus_breakdown: ConsensusBreakdown;
  thesis: string;
  updated_at: string;
  created_at: string;
}

export interface SignalsResponse {
  items: SignalCard[];
  count: number;
  active_signals: number;
  consensus: ConsensusBreakdown & { label: ConsensusLabel };
  caller: PublicCaller;
}

// /signals/{id} — adversarial framing

export interface AdversarialView {
  bull: AgentSpeakBlock | null;
  bear: AgentSpeakBlock | null;
  commander: AgentSpeakBlock | null;
}

export interface AgentSpeakBlock {
  label: string;
  stance: "LONG" | "SHORT" | "ABSTAIN";
  confidence: number;        // 0-100
  notes: string;
}

// /signals/{id} — governance framing (Strategist/Auditor/Synthesized)

export interface GovernanceView {
  strategist: {
    label: "STRATEGIST_AGENT";
    proposal: string;        // "PROPOSE LONG" / "PROPOSE HOLD" / "PROPOSE SHORT"
    confidence: number;
    detected: string;
  };
  auditor: {
    label: "RISK_AUDITOR_AGENT";
    action: "PASS" | "VETO";
    mode: string;            // "NO_THREAT_DETECTED" | "DISSENT_AGAINST_STRATEGIST" | "AWAITING_REVIEW" | "ALIGNED"
    confidence: number;
  };
  synthesized: {
    label: "SYNTHESIZED SIGNAL";
    symbol: string;
    direction: "LONG" | "SHORT" | "HOLD";
    confidence: number;
  };
}

export interface SignalDetail extends SignalCard {
  adversarial: AdversarialView;
  governance: GovernanceView;
  caller: PublicCaller;
}

// ──────────────────────── /digest ────────────────────────

export interface DigestPrediction {
  symbol: string;
  direction: "LONG" | "SHORT" | "HOLD" | "NO_TRADE";
  confidence: number;
  price: number | null;
}

export interface DigestSmartMoney {
  symbol: string;
  score: number;
  signal: "bullish" | "bearish" | "neutral";
  net_flow_usd: number | null;
  bullish: number;
  bearish: number;
}

export interface DigestAlert {
  symbol: string;
  delta: number;
  signal_change: string;
}

export interface LockedRow {
  symbol: null;
  locked: true;
  kind: "predictions" | "smart_money" | "alerts";
  upgrade_to: Tier;
  cta: string;
}

export interface DigestResponse {
  predictions: (DigestPrediction | LockedRow)[];
  smart_money: (DigestSmartMoney | LockedRow)[];
  alerts: (DigestAlert | LockedRow)[];
  overview: { active_signals: number; summary: string };
  watchlist: null;    // filled by risedual.ai from its own watchlist store
  caps: { predictions: number; smart_money: number; alerts: number };
  tier: Tier;
}

export const isLockedRow = (row: unknown): row is LockedRow =>
  typeof row === "object" && row !== null && (row as LockedRow).locked === true;

// ──────────────────────── /digest/narrative ────────────────────────

export interface NarrativeResponse {
  text: string;
  cached: boolean;
  generated_at: string;
  model: string;            // "gemini:gemini-3-flash-preview"
  tier: Tier;
}

// ──────────────────────── /chat ────────────────────────

export interface ChatRequest {
  message: string;
  session_id?: string;
}

export interface ChatResponse {
  session_id: string;
  reply: string;
  model: string;            // "anthropic:claude-sonnet-4-5-20250929"
  tier: Tier;               // always "pro_max"
  turn_count: number;
  new_session: boolean;
}

export interface ChatMessage {
  message_id: string;
  session_id: string;
  role: "user" | "assistant";
  text: string;
  ts: string;
}

export interface ChatHistoryResponse {
  session_id: string;
  messages: ChatMessage[];
  count: number;
  tier: Tier;
}

// ──────────────────────── /scanner ────────────────────────

export type ScannerPresetId =
  | "macd_bullish_cross"
  | "macd_bearish_cross"
  | "bollinger_squeeze"
  | "ema_golden_cross"
  | "volume_spike"
  | "near_52w_high"
  | "near_52w_low"
  | "rsi_overbought"
  | "rsi_oversold"
  | "momentum_breakout";

export interface ScannerPreset {
  preset_id: ScannerPresetId;
  name: string;
  category: string;
  signal: "bullish" | "bearish" | "neutral";
}

export interface ScannerMatch {
  symbol: string;
  strength: number;       // 0-100
  detail: string;
}

export interface ScannerPresetsResponse {
  presets: ScannerPreset[];
  count: number;
  tier: Tier;
}

export interface ScannerScanResponse {
  preset_id: ScannerPresetId;
  name: string;
  category: string;
  signal: "bullish" | "bearish" | "neutral";
  matches: ScannerMatch[];
  scanned: number;
  matched: number;
}

// ──────────────────────── /agent-activity ────────────────────────

export type AgentActivityType =
  | "signal_proposed"
  | "stance_posted"
  | "paper_trade_open"
  | "paper_trade_skip"
  | "prediction_resolved"
  | "info";

export type AgentActivitySeverity = "info" | "success" | "warn" | "error";

export interface AgentActivityEvent {
  event_id: string;
  timestamp: string;
  type: AgentActivityType;
  severity: AgentActivitySeverity;
  title: string;
  detail: string | null;
  symbol: string | null;
  metadata: Record<string, unknown>;
}

export interface AgentActivityResponse {
  items: AgentActivityEvent[];
  count: number;
  since: string | null;
  polled_at: string;
  tier: Tier;
}

// ──────────────────────── /models-mind ────────────────────────

export interface FeatureScalar {
  score: number | null;
  value: number | string | null;
  coverage?: "live" | "not_wired";
  units?: string;
  label?: string;
  direction?: "up" | "down";
}

export interface ModelsMindFeatures {
  score_2W: FeatureScalar | null;
  distance_from_mw: FeatureScalar | null;
  macro_regime_flag: FeatureScalar | null;
  atr_id: FeatureScalar | null;
  earnings_proximity: FeatureScalar | null;     // coverage: not_wired today
  momentum_3d: FeatureScalar | null;
  sector_rs: FeatureScalar | null;              // coverage: not_wired today
  pattern_score: FeatureScalar | null;
  rsi_id: FeatureScalar | null;
  vol_zscore: FeatureScalar | null;
}

export interface ModelsMindResponse {
  symbol: string;
  source: string;
  tf: string;
  last_close: number | null;
  last_bar_ts: string | null;
  features: ModelsMindFeatures;
  computed_at: string;
  tier: Tier;
}

// ──────────────────────── /heatmap + /sectors ────────────────────────

export type ColorBand = "strong_buy" | "mild_buy" | "neutral" | "mild_sell" | "strong_sell";

export interface HeatmapItem {
  symbol: string;
  change_24h_pct: number;
  color_band: ColorBand;
  source: string;
}

export interface HeatmapResponse {
  items: HeatmapItem[];
  count: number;
  degraded: boolean;
  tier: Tier;
}

export interface SectorItem {
  symbol: string;
  name: string;
  change_24h_pct: number | null;
  color_band: ColorBand;
  coverage: "live" | "not_wired";
}

export interface SectorsResponse {
  items: SectorItem[];
  best: SectorItem | null;
  worst: SectorItem | null;
  degraded: boolean;
  tier: Tier;
}
