import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, RUNTIME_META, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import QuickSeatSwitches from "@/components/QuickSeatSwitches";
import PublicConnect from "@/components/PublicConnect";
import KrakenBrokerTile from "@/components/KrakenBrokerTile";
import LaneRoutingPill from "@/components/LaneRoutingPill";
import MasterTradingSwitch from "@/components/MasterTradingSwitch";
import WebullEntitlementsCard from "@/components/WebullEntitlementsCard";
import WebullOtocoTestPanel from "@/components/WebullOtocoTestPanel";
import WebullOtocoLivePanel from "@/components/WebullOtocoLivePanel";
import TraderPostMortem from "@/components/TraderPostMortem";
import InputProvenanceBadge from "@/components/InputProvenanceBadge";
import ExecutionScoreBreakdown from "@/components/ExecutionScoreBreakdown";
// 2026-07-01 (Pass 2/3 cleanup, batch 1): removed imports for panels
// whose backend endpoints were deleted — LegacyWrapperTogglePanel,
// AutoSubmitPolicyPanel, OperatorInjectIntent, SubmitOrderModal.
// Sidecar Trader owns execution; MC is eyes-only on this page.
//
// 2026-07-01 (batch 2): additionally removed TunablesStrip
// (/admin/auto-submit/tunables-simulator → 404) and
// SeatRosterStrip (its backend query hits Atlas directly and
// times out on the shared-tier connection; the Overview Trader
// Seats tile already surfaces the same info via the
// Mongo-independent in-memory state cache).
import ParabolicPhaseStrip from "@/components/ParabolicPhaseStrip";
import BrokerSelectionMenu from "@/components/BrokerSelectionMenu";
import DoctrineStrip from "@/components/DoctrineStrip";
import AutoRetireStrip from "@/components/AutoRetireStrip";
import DoctrineHealthPanel from "@/components/DoctrineHealthPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import PipelineBlockerChip from "@/components/PipelineBlockerChip";
import SeatRegistryDriftBanner from "@/components/SeatRegistryDriftBanner";
import { toast } from "sonner";
import {
  Lightning, ArrowsClockwise, Funnel, Pulse,
  CheckCircle, XCircle, Hourglass, CaretDown, CaretUp,
  CurrencyBtc, Buildings,
} from "@phosphor-icons/react";

const BRAIN_META = {
  ...RUNTIME_META,
  gto: { label: "GTO", color: "#DC2626" },
};

const ACTION_COLOR = {
  BUY: "#10B981",
  SELL: "#DC2626",
  SHORT: "#A78BFA",
  COVER: "#3B82F6",
  HOLD: "#A1A1AA",
};

const GATE_COLOR = {
  pending: "#A1A1AA",
  passed: "#10B981",
  blocked: "#DC2626",
  dry_run_passed: "#10B981",
  dry_run_blocked: "#F59E0B",
};

const GATE_ICON = {
  pending: Hourglass,
  passed: CheckCircle,
  blocked: XCircle,
  dry_run_passed: CheckCircle,
  dry_run_blocked: XCircle,
};

const SORTS = [
  { value: "conviction",          label: "🔥 Highest Conviction" },
  { value: "execution_priority",  label: "🚦 Closest to Execution" },
  { value: "newest",              label: "🕒 Newest" },
  { value: "symbol",              label: "🔤 Symbol (A-Z)" },
];

const STACKS = ["all", "camino", "barracuda", "hellcat", "gto"];
const ACTIONS = ["all", "BUY", "SELL", "SHORT", "COVER", "HOLD"];
const LANES = ["all", "equity", "crypto"];
const GATE_STATES = ["all", "pending", "passed", "blocked", "dry_run_passed", "dry_run_blocked", "rejected_at_ingest"];

function SectionDivider({ title, sub, icon: IconComponent, testid, rightSlot }) {
  return (
    <div
      className="mt-6 mb-3 flex items-baseline gap-3 border-t border-rd-border pt-4"
      data-testid={testid}
    >
      {IconComponent && (
        <IconComponent size={14} weight="bold" className="text-rd-text shrink-0" />
      )}
      <div className="min-w-0 flex-1">
        <div className="text-[11px] font-mono uppercase tracking-[0.25em] text-rd-text">
          {title}
        </div>
        {sub && (
          <div className="text-[10px] font-mono text-rd-dim mt-1 leading-relaxed">
            {sub}
          </div>
        )}
      </div>
      {rightSlot && (
        <div className="shrink-0 self-baseline">{rightSlot}</div>
      )}
    </div>
  );
}

function FilterPill({ label, value, options, onChange, testid }) {
  return (
    <div className="flex items-center gap-1.5" data-testid={testid}>
      <span className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</span>
      <div className="flex rounded-sm border border-rd-border bg-rd-bg p-0.5">
        {options.map((opt) => {
          const active = opt === value;
          return (
            <button
              key={opt}
              onClick={() => onChange(opt)}
              data-testid={`${testid}-${opt}`}
              className={
                "px-2 py-1 text-[10px] font-mono uppercase tracking-wide rounded-sm transition-colors " +
                (active
                  ? "bg-rd-accent text-black"
                  : "text-rd-dim hover:text-rd-text")
              }
            >
              {opt}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function StatTile({ label, value, color, testid }) {  return (
    <div
      className="border border-rd-border bg-rd-bg p-3"
      data-testid={testid}
    >
      <div className="flex items-center gap-2 mb-1.5">
        <span
          className="inline-block w-1.5 h-1.5 rounded-full"
          style={{ background: color || "#A1A1AA" }}
        />
        <span className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</span>
      </div>
      <div className="font-display text-xl font-bold text-rd-text leading-none">{value}</div>
    </div>
  );
}

function IntentRow({ intent, expanded, onToggle, onDryRun, dryRunResult }) {
  const meta = BRAIN_META[intent.stack] || { label: intent.stack, color: "#A1A1AA" };
  const GateIcon = GATE_ICON[intent.gate_state] || Hourglass;
  const gateColor = GATE_COLOR[intent.gate_state] || "#A1A1AA";
  const actionColor = ACTION_COLOR[intent.action] || "#A1A1AA";
  const isExecuted = intent.executed === true;

  return (
    <>
      <tr
        className="border-b border-rd-border hover:bg-rd-bg cursor-pointer transition-colors"
        onClick={onToggle}
        data-testid={`intent-row-${intent.intent_id}`}
      >
        <td className="px-3 py-2 font-mono text-[10px] text-rd-muted whitespace-nowrap">
          {relTime(intent.ingest_ts)}
        </td>
        <td className="px-3 py-2">
          <Badge color={meta.color}>{meta.label}</Badge>
        </td>
        <td className="px-3 py-2 font-display text-sm text-rd-text">
          <div className="flex items-center gap-2">
            <span>{intent.symbol}</span>
            <InputProvenanceBadge intent={intent} />
          </div>
        </td>
        <td className="px-3 py-2">
          {intent.lane ? (
            <Badge color={intent.lane === "crypto" ? "#A855F7" : "#3B82F6"}>
              {intent.lane.toUpperCase()}
            </Badge>
          ) : (
            <span className="font-mono text-[10px] text-rd-dim">—</span>
          )}
        </td>
        <td className="px-3 py-2">
          <span
            className="font-mono text-[11px] font-bold tracking-wider"
            style={{ color: actionColor }}
          >
            {intent.action}
          </span>
        </td>
        <td className="px-3 py-2 font-mono text-xs text-rd-text">
          {Number(intent.confidence).toFixed(3)}
        </td>
        <td className="px-3 py-2 font-mono text-xs text-rd-text">
          {Number(intent.risk_multiplier).toFixed(3)}
        </td>
        <td className="px-3 py-2">
          <span
            className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-wider"
            style={{ color: gateColor }}
          >
            <GateIcon size={11} weight="bold" />
            {intent.gate_state}
          </span>
        </td>
        <td className="px-3 py-2 text-right">
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={(e) => { e.stopPropagation(); onDryRun(); }}
              data-testid={`intent-dryrun-${intent.intent_id}`}
              className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
              title="Run gate chain against this intent (no broker call)"
            >
              <Lightning size={10} weight="bold" className="inline mr-1" />
              dry-run
            </button>
            {isExecuted && (
              <span
                className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-success text-rd-success"
                data-testid={`intent-executed-${intent.intent_id}`}
                title="Already executed by the trader"
              >
                <CheckCircle size={10} weight="bold" className="inline mr-1" />
                executed
              </span>
            )}
            {expanded ? (
              <CaretUp size={12} weight="bold" className="text-rd-dim" />
            ) : (
              <CaretDown size={12} weight="bold" className="text-rd-dim" />
            )}
          </div>
        </td>
      </tr>
      {intent.doctrine_packet && (
        <tr
          className="border-b border-rd-border"
          data-testid={`intent-doctrine-row-${intent.intent_id}`}
        >
          <td colSpan={9} className="p-0">
            <PanelErrorBoundary
              panelName="Doctrine"
              testid={`panel-error-doctrine-${intent.intent_id}`}
              compact
            >
              <div className="space-y-3 p-3 bg-rd-bg2 border-b border-rd-border">
                <ExecutionScoreBreakdown intent={intent} />
                <DoctrineStrip
                  packet={intent.doctrine_packet}
                  intentId={intent.intent_id}
                />
              </div>
            </PanelErrorBoundary>
          </td>
        </tr>
      )}
      {expanded && (
        <tr className="bg-rd-bg" data-testid={`intent-detail-${intent.intent_id}`}>
          <td colSpan={9} className="px-3 py-4">
            <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-6">
              <div>
                <div className="label-eyebrow mb-2">Rationale</div>
                <p className="text-xs text-rd-text leading-relaxed font-mono whitespace-pre-wrap break-words">
                  {intent.rationale || "—"}
                </p>
                {intent.evidence && Object.keys(intent.evidence).length > 0 && (
                  <>
                    <div className="label-eyebrow mt-4 mb-2">Evidence</div>
                    <pre className="text-[11px] text-rd-text font-mono bg-rd-bg2 border border-rd-border p-3 overflow-x-auto leading-relaxed">
                      {JSON.stringify(intent.evidence, null, 2)}
                    </pre>
                  </>
                )}
                {dryRunResult && (
                  <>
                    <div className="label-eyebrow mt-4 mb-2">
                      Dry-run verdict ·{" "}
                      <span style={{
                        color: dryRunResult.verdict === "would_pass" ? "#10B981" : "#F59E0B",
                      }}>
                        {dryRunResult.verdict?.replace("_", " ")?.toUpperCase()}
                      </span>
                    </div>
                    <div className="border border-rd-border bg-rd-bg2 divide-y divide-rd-border">
                      {(dryRunResult.gates || []).map((g) => (
                        <div key={g.name} className="px-3 py-2 flex items-start gap-3" data-testid={`gate-${g.name}`}>
                          {g.passed ? (
                            <CheckCircle size={13} weight="bold" className="text-rd-success mt-0.5 shrink-0" />
                          ) : (
                            <XCircle size={13} weight="bold" className="text-rd-danger mt-0.5 shrink-0" />
                          )}
                          <div className="flex-1 min-w-0">
                            <div className="font-mono text-[11px] text-rd-text">{g.name}</div>
                            <div className="text-[10px] text-rd-muted leading-relaxed mt-0.5">{g.reason}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                )}
                {/* Submit result block removed 2026-07-01 — MC no longer
                    routes intents to the broker. The Sidecar Trader
                    (background asyncio task) owns all execution and
                    writes to `executions.sqlite` + Mongo mirror. */}
              </div>
              <div className="text-[11px] font-mono space-y-2">
                <div className="label-eyebrow mb-2">Stamped by MC</div>
                {[
                  ["intent_id", intent.intent_id],
                  ["seat_at_post_time", intent.seat_at_post_time || "—"],
                  ["ingest_method", intent.ingest_method || "—"],
                  ["regime", intent.regime || "—"],
                  ["decision_id", intent.decision_id || "—"],
                  ["may_execute", String(intent.may_execute)],
                  ["requires_gate_pass", String(intent.requires_gate_pass)],
                  ["executed", String(intent.executed)],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-2 border-b border-rd-border pb-1">
                    <span className="text-rd-dim">{k}</span>
                    <span className="text-rd-text truncate" title={String(v)}>{String(v)}</span>
                  </div>
                ))}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export default function Intents() {
  const [intents, setIntents] = useState(null);
  const [err, setErr] = useState("");
  const [stack, setStack] = useState("all");
  const [action, setAction] = useState("all");
  const [gateState, setGateState] = useState("all");
  const [lane, setLane] = useState("all");
  // 2026-02-23 operator queue improvements
  //   sort: conviction (default — highest confidence first; operator's
  //         strongest ideas surface immediately when they open the page),
  //         execution_priority (BUY/SELL passed → blocked → HOLD),
  //         newest (legacy ingest_ts DESC), symbol (alphabetical, escape
  //         hatch for ticker lookup — explicitly NOT the default since
  //         it hides the most important opportunities).
  //   showDisabledLanes: when crypto (or any lane) is paused, hide its
  //         intents from the operator queue by default. Toggle ON to
  //         inspect them for QA / forensics.
  const [sort, setSort] = useState("conviction");
  const [showDisabledLanes, setShowDisabledLanes] = useState(false);
  const [enabledLanes, setEnabledLanes] = useState([]);
  const [queueNote, setQueueNote] = useState("");
  const [expanded, setExpanded] = useState(null);
  const [dryRunByIntent, setDryRunByIntent] = useState({});
  const [autoRefresh, setAutoRefresh] = useState(true);
  // 2026-07-01: submit/caps/modal state removed. MC is eyes-only;
  // the Sidecar Trader owns execution. Dry-run stays because it's a
  // pure gate-chain probe that never touches the broker.

  const load = useCallback(async () => {
    try {
      const params = { limit: 100, sort };
      if (stack !== "all") params.stack = stack;
      if (gateState !== "all") params.gate_state = gateState;
      if (lane !== "all") params.lane = lane;
      // 2026-02-23 — only send the flag when the operator wants the
      // disabled-lane intents shown. Default is server-side false,
      // keeping the actionable queue clean of crypto noise while
      // the lane is paused.
      if (showDisabledLanes) params.include_disabled_lanes = "true";
      // action filter happens client-side since the API doesn't expose it
      // 2026-02-21: Stale `X-Runtime-Token: alpha-ingest-...` header
      // removed — that token belonged to the deleted sidecar HTTP
      // plumbing and caused an HTTP 401 red bar on this page. The
      // backend `/intents` GET now accepts admin JWT directly.
      const res = await api.get("/intents", { params });
      setIntents(res.data?.items || []);
      setEnabledLanes(res.data?.enabled_lanes || []);
      setQueueNote(res.data?.note || "");
      setErr("");
    } catch (e) {
      // 2026-06-18: tag the error with the endpoint that failed so a
      // bare "HTTP 500" on the page is at least actionable. Without
      // this, the operator sees just "HTTP 500" with no clue which
      // endpoint to grep for in backend logs.
      const detail = e?.response?.data?.detail || e.message || "unknown";
      const reqId = e?.response?.data?.request_id;
      const status = e?.response?.status;
      setErr(
        `GET /api/intents → ${status ? `HTTP ${status}` : "(no response)"}: ${detail}` +
        (reqId ? ` · request_id=${reqId}` : ""),
      );
    }
  }, [stack, gateState, lane, sort, showDisabledLanes]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [autoRefresh, load]);

  const filtered = useMemo(() => {
    if (!intents) return null;
    if (action === "all") return intents;
    return intents.filter((it) => it.action === action);
  }, [intents, action]);

  const stats = useMemo(() => {
    if (!filtered) return null;
    const byStack = {};
    const byGate = {};
    const byAction = {};
    for (const it of filtered) {
      byStack[it.stack] = (byStack[it.stack] || 0) + 1;
      byGate[it.gate_state] = (byGate[it.gate_state] || 0) + 1;
      byAction[it.action] = (byAction[it.action] || 0) + 1;
    }
    return { total: filtered.length, byStack, byGate, byAction };
  }, [filtered]);

  const runDryRun = async (intentId) => {
    setDryRunByIntent((m) => ({ ...m, [intentId]: { loading: true } }));
    try {
      const res = await api.post(`/execution/dry_run?intent_id=${encodeURIComponent(intentId)}`);
      setDryRunByIntent((m) => ({ ...m, [intentId]: res.data }));
      setExpanded(intentId);
      // refresh to pick up gate_state change
      load();
    } catch (e) {
      setDryRunByIntent((m) => ({
        ...m,
        [intentId]: { error: e?.response?.data?.detail || e.message },
      }));
    }
  };

  // 2026-07-01: runSubmit/performSubmit removed. Sidecar trader owns
  // execution — no manual submit button on this page anymore.

  return (
    <div className="reveal" data-testid="intents-page">
      <PageHeader
        eyebrow="Decision Machine"
        title="Intents"
        sub="Intent envelopes emitted by the four brains. Every intent is a candidate; MC's gate chain decides if it lives. Schema pins may_execute=false and requires_gate_pass=true — brains cannot route an order through this surface."
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setAutoRefresh((v) => !v)}
              data-testid="intents-autorefresh"
              className={
                "px-2 py-1 text-[10px] font-mono uppercase tracking-wider border font-bold " +
                (autoRefresh
                  ? "border-rd-success bg-rd-success text-black shadow-[0_0_10px_rgba(34,197,94,0.55)]"
                  : "border-rd-border text-rd-dim")
              }
              title="Auto-refresh every 8s"
            >
              <Pulse size={10} weight="fill" className="inline mr-1" />
              {autoRefresh ? "LIVE · 8s" : "paused"}
            </button>
            <button
              onClick={load}
              data-testid="intents-reload"
              className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
              title="Reload now"
            >
              <ArrowsClockwise size={11} weight="bold" />
            </button>
          </div>
        }
        testid="intents-header"
      />

      {/* ─── Seat registry drift banner (2026-02-17, pass #48) ───
          Surfaces any roster ↔ legacy executor_seat mismatch (or a
          vacant execute-seat in any lane) so the operator sees the
          desync immediately — instead of days of executor_seat_check
          blocks piling up under a stale legacy holder. Read-only;
          polls /admin/seat-registry/diagnose every 30s. */}
      <SeatRegistryDriftBanner />

      {/* ─── Why are we not trading? (2026-02-19, prod-incident smoking gun) ───
          Single tile that answers the operator's persistent question:
          which gate/layer is killing intents and how often. Surfaces
          the dominant failure mode + top blockers ranked by frequency
          so we stop guessing. Always visible at the top — this is
          the single most important panel for diagnosing prod. */}
      <div className="mb-4" data-testid="intents-receipts-mount">
        <PanelErrorBoundary label="Receipts">
          <TraderPostMortem />
        </PanelErrorBoundary>
      </div>

      {/* ─── Auto-Submit Policy panel removed 2026-07-01 — its backend
          endpoint /admin/auto-submit/policy was deleted in Pass 2/3.
          Auto-execution is now the Sidecar Trader's responsibility;
          it fires when TRADER_ENABLED=true, no manual policy toggle. */}

      {/* ─── Tunables what-if strip removed 2026-07-01 —
          /admin/auto-submit/tunables-simulator was deleted with the
          rest of the auto-submit policy machinery. Sidecar Trader
          fires when TRADER_ENABLED=true; no tunables to simulate. */}

      {/* ─── Seat Roster strip removed 2026-07-01 — its backend query
          hits Atlas directly and times out on the shared-tier
          connection. The Overview Trader Seats tile (4×2 grid, angel
          names, executor highlight) already surfaces the same info,
          served from the Mongo-independent in-memory state cache. */}

      {/* ─── Quick Seat Switches (2026-05-27, pass #17) ───
          One-click seat assignment for all 4 seats per lane. Uses
          /api/admin/roster/assign — the actual assignment surface. */}
      <div className="mb-4" data-testid="intents-quick-switches-mount">
        <PanelErrorBoundary label="Quick Seat Switches">
          <QuickSeatSwitches />
        </PanelErrorBoundary>
      </div>

      {/* ─── Twin authority lanes ─── Doctrine: equity and crypto are
          symmetric. Each lane has its own broker tile and exposure
          caps. Seat assignment for all 4 seats × 2 lanes is handled
          by the QuickSeatSwitches panel above — the legacy per-seat
          dedicated tiles (ExecutorSeatTile, AuditorSeatTile,
          RosterSeatTile) were removed 2026-05-27 (pass #18) per
          operator request to eliminate redundant assignment surfaces. */}
      <SectionDivider
        title="Master Trading Switch"
        icon={Buildings}
        sub="Global kill switch. When OFF, the auto-router short-circuits on every tick regardless of lane / seat / broker state. OFF is one-click; ON requires confirmation + reason."
        testid="intents-section-master-switch"
      />
      <MasterTradingSwitch />

      <SectionDivider
        title="Equity Lane"
        icon={Buildings}
        sub="Webull-routed equity execution. Public.com and Alpaca are deprecated. Seat assignment lives in Quick Seat Switches above."
        testid="intents-section-equity"
        rightSlot={<LaneRoutingPill lane="equity" />}
      />
      <WebullEntitlementsCard />

      {/* Atomic OTOCO bracket — P1 Phase 2 (2026-02-19). Whole-share
          only; operator-driven so we can observe Webull's combo
          lifecycle before wiring this into the auto-router. */}
      <div className="mt-3">
        <PanelErrorBoundary panelName="Webull OTOCO" testid="panel-error-otoco">
          <WebullOtocoTestPanel />
        </PanelErrorBoundary>
      </div>

      {/* Live OTOCO tile — polls Webull's v3 open-orders API and
          groups the rows into bracket envelopes so the operator can
          watch the MASTER + TP + SL legs without opening the Webull
          mobile app. */}
      <div className="mt-3">
        <PanelErrorBoundary panelName="Webull OTOCO Live" testid="panel-error-otoco-live">
          <WebullOtocoLivePanel />
        </PanelErrorBoundary>
      </div>

      {/* Legacy wrapper A/B toggle removed 2026-07-01 — the
          /admin/wrappers endpoint was deleted in Pass 2/3 (the whole
          legacy-wrapper concept is gone; the sidecar trader talks
          directly to Webull). */}

      <SectionDivider
        title="Crypto Lane"
        icon={CurrencyBtc}
        sub="Kraken-routed crypto execution. Seat assignment lives in Quick Seat Switches above. All crypto seats empty by default — operator must assign before any crypto trade can fire."
        testid="intents-section-crypto"
        rightSlot={<LaneRoutingPill lane="crypto" />}
      />
      <KrakenBrokerTile />
      <div className="mt-3">
        <BrokerSelectionMenu />
      </div>
      <div className="mt-3">
        <ParabolicPhaseStrip />
      </div>

      {/* Live exposure caps strip removed 2026-07-01 — /config/exposure-caps
          was deleted in Pass 2/3. The Sidecar Trader's caps
          (TRADER_PER_ORDER_USD_CAP + TRADER_DAILY_USD_CAP) show on
          the Overview → Trade Tape tile instead. */}

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-2 mb-6" data-testid="intents-stats">
          <StatTile label="Total" value={stats.total} testid="stat-total" />
          {STACKS.filter((s) => s !== "all").map((s) => (
            <StatTile
              key={s}
              label={s.toUpperCase()}
              value={stats.byStack[s] || 0}
              color={BRAIN_META[s]?.color}
              testid={`stat-stack-${s}`}
            />
          ))}
          <StatTile
            label="Pending"
            value={stats.byGate.pending || 0}
            color="#A1A1AA"
            testid="stat-pending"
          />
        </div>
      )}

      {/* Pipeline blocker histogram — at-a-glance "why intents aren't
          flowing" with one-tap fix links. Lives just above the filter
          strip so it's the first thing the operator sees. */}
      <PanelErrorBoundary panelName="Pipeline Blocker Chip" testid="panel-error-blocker-chip">
        <PipelineBlockerChip />
      </PanelErrorBoundary>

      {/* Filters */}
      <Card className="mb-4" testid="intents-filters">
        <div className="flex items-center gap-3 mb-3">
          <Funnel size={13} weight="bold" className="text-rd-dim" />
          <span className="label-eyebrow">Filters</span>
        </div>
        <div className="flex flex-wrap gap-4">
          <FilterPill label="Lane" value={lane} options={LANES} onChange={setLane} testid="filter-lane" />
          <FilterPill label="Stack" value={stack} options={STACKS} onChange={setStack} testid="filter-stack" />
          <FilterPill label="Action" value={action} options={ACTIONS} onChange={setAction} testid="filter-action" />
          <FilterPill label="Gate" value={gateState} options={GATE_STATES} onChange={setGateState} testid="filter-gate" />
        </div>
        {/* 2026-02-23 — operator queue improvements. Sort selector +
            disabled-lane visibility toggle. Highest-conviction is the
            default so the operator sees their strongest ideas first,
            not the first ticker beginning with 'A'. Crypto intents
            are hidden from the actionable queue while crypto lane
            execution is OFF (brains still emit advisor opinions on
            crypto — they just don't pollute the operator's view). */}
        <div className="flex flex-wrap items-center gap-4 mt-3 pt-3 border-t border-rd-border">
          <div className="flex items-center gap-2" data-testid="intents-sort-selector">
            <span className="label-eyebrow">Sort</span>
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value)}
              className="bg-rd-bg-deep border border-rd-border text-rd-text text-xs font-mono px-2 py-1 focus:outline-none focus:border-rd-accent"
              data-testid="intents-sort-select"
            >
              {SORTS.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>
          <label className="flex items-center gap-2 text-xs font-mono text-rd-dim cursor-pointer"
                 data-testid="intents-show-disabled-toggle">
            <input
              type="checkbox"
              checked={showDisabledLanes}
              onChange={(e) => setShowDisabledLanes(e.target.checked)}
              className="accent-rd-accent"
              data-testid="intents-show-disabled-checkbox"
            />
            <span>Show disabled-lane intents</span>
          </label>
          {enabledLanes.length > 0 && !showDisabledLanes && (
            <span
              className="text-[10px] font-mono uppercase tracking-wider text-rd-dim ml-auto"
              data-testid="intents-enabled-lanes-pill"
              title="Only intents on currently-executing lanes are shown. Toggle 'Show disabled-lane intents' to inspect others."
            >
              enabled lanes: {enabledLanes.join(" · ")}
            </span>
          )}
          {queueNote && (
            <span
              className="text-[10px] font-mono text-rd-warn"
              data-testid="intents-queue-note"
            >
              {queueNote}
            </span>
          )}
        </div>
      </Card>

      {/* Seat-doctrinal auto-retire suggestions. Lane-scoped so the
          operator only sees suggestions for the lane they're filtering. */}
      <PanelErrorBoundary panelName="Auto-Retire Strip" testid="panel-error-autoretire">
        <AutoRetireStrip lane={lane} />
      </PanelErrorBoundary>

      {/* Live doctrine-health summary. Compact mode keeps it scannable
          alongside the auto-retire strip; the full /admin/doctrine page
          has the deep view with ideal-snapshot + blockers + rejections. */}
      <PanelErrorBoundary panelName="Doctrine Health" testid="panel-error-doctrine-health">
        <DoctrineHealthPanel mode="compact" lane={lane} />
      </PanelErrorBoundary>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono" data-testid="intents-error">
          {err}
        </div>
      )}

      {/* Table */}
      <Card className="p-0" testid="intents-table-card">
        {filtered === null ? (
          <LoadingRow />
        ) : filtered.length === 0 ? (
          <EmptyState message="No intents match these filters." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left" data-testid="intents-table">
              <thead className="bg-rd-bg border-b border-rd-border">
                <tr className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
                  <th className="px-3 py-2 font-normal">When</th>
                  <th className="px-3 py-2 font-normal">Stack</th>
                  <th className="px-3 py-2 font-normal">Symbol</th>
                  <th className="px-3 py-2 font-normal">Lane</th>
                  <th className="px-3 py-2 font-normal">Action</th>
                  <th className="px-3 py-2 font-normal">Conf</th>
                  <th className="px-3 py-2 font-normal">R·Mult</th>
                  <th className="px-3 py-2 font-normal">Gate</th>
                  <th className="px-3 py-2 font-normal text-right">—</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((it) => (
                  <IntentRow
                    key={it.intent_id}
                    intent={it}
                    expanded={expanded === it.intent_id}
                    onToggle={() => setExpanded((e) => (e === it.intent_id ? null : it.intent_id))}
                    onDryRun={() => runDryRun(it.intent_id)}
                    dryRunResult={dryRunByIntent[it.intent_id]}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* SubmitOrderModal removed 2026-07-01 — no manual submit path. */}
    </div>
  );
}
