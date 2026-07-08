import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, getRuntimeMeta, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";
import VRLScorecardsPanel from "@/components/VRLScorecardsPanel";
import SidecarCheckinPanel from "@/components/SidecarCheckinPanel";
import LaneExecutionTogglesPanel from "@/components/LaneExecutionTogglesPanel";
import BracketOutcomeDistributionPanel from "@/components/BracketOutcomeDistributionPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import BrainDeepDiagnoseCard from "@/components/BrainDeepDiagnoseCard";
// 2026-07-01 (Pass 2/3 cleanup, batch 6): removed 3 tiles that
// hit Atlas heavy queries and timed out on shared-tier:
//   * BrainMetricsTile        (MaxTimeMSExpired on multi-day aggregate)
//   * SeatStageDropsTile      (endpoint 404 / Not Found)
//   * ExecutionLifecycleFunnelTile (NetworkTimeout on shared-shard)
// The Sidecar Trader's Trade Tape + Post-Mortem panels serve the
// same operator question ("what did the trader do / why didn't it
// fire?") from local SQLite, Mongo-independent.
import AdvisorPerformanceTile from "@/components/AdvisorPerformanceTile";
import NativeBrainRuntimeTile from "@/components/NativeBrainRuntimeTile";  // 2026-02-23 in-process brain migration
import BrainInputHealthTile from "@/components/BrainInputHealthTile";  // 2026-02-23 instrument quality
import HealthcheckTile from "@/components/HealthcheckTile";  // 2026-02-26 post-deploy validation
import FingerprintDiffPanel from "@/components/FingerprintDiffPanel";  // 2026-02-20 doctrine-change before/after
// ImposterScanCard removed 2026-02-21: the sidecar HTTP brain plumbing
// it monitored was deleted (brains run in-process now), and the
// `/admin/runtime/sidecar-imposter-scan` endpoint went with it — the
// stale tile was throwing HTTP 404 on every page load.
// 2026-07-06 — deleted three dead-on-arrival tiles per operator
// directive: DecisionsFeed, PromotionArtifactPanel, BrainHealthTile.
// All three had been throwing Mongo Atlas timeouts in production
// since install and had no operational value that wasn't already
// served by other surfaces (Intent Clearance Funnel, per-collection
// direct queries, sidecar-checkin + opinion-watchdog endpoints).
// Removing them cuts three heavy Atlas queries per page load.

const BRAINS_FOR_FILTER = ["all", "camino", "barracuda", "hellcat", "gto"];

/**
 * LazyDetails — `<details>`-based collapsible panel that defers
 * mounting (and therefore fetching) its children until the user
 * actually opens it. Used to drop the Diagnostics page's mount cost
 * by ~40% (2026-02-19) — rare-use panels like Quantum, VRL Scorecards,
 * SidecarCheckin, and BracketOutcomes now only fetch on demand.
 *
 * Once opened, the child stays mounted (typical `<details>` semantics)
 * so the next open is instant. To force an unmount on close, the
 * operator can refresh the page — by design.
 */
function LazyDetails({ summary, defaultOpen = false, children, testid }) {
  const [hasOpened, setHasOpened] = React.useState(defaultOpen);
  return (
    <details
      className="mt-6 border border-rd-border bg-rd-bg"
      data-testid={testid}
      open={defaultOpen}
      onToggle={(e) => { if (e.target.open) setHasOpened(true); }}
    >
      <summary className="cursor-pointer select-none px-4 py-2.5 text-[11px] font-mono uppercase tracking-widest text-rd-dim hover:text-rd-text">
        {summary}
      </summary>
      <div className="border-t border-rd-border">
        {hasOpened ? children : null}
      </div>
    </details>
  );
}

const REGIME_COLOR = {
  trend_up:    "#10B981",
  trend_down:  "#DC2626",
  panic:       "#EF4444",
  squeeze:     "#A855F7",
  mean_revert: "#F59E0B",
  neutral:     "#A1A1AA",
};

function QuantumPanel() {
  const [items, setItems] = useState(null);
  const [counters, setCounters] = useState({});
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/quantum/recent", { params: { limit: 30 } });
      setItems(data?.items || []);
      setCounters(data?.counters || {});
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <Card className="p-0 overflow-hidden" testid="quantum-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 border-b border-rd-border bg-rd-bg3">
        <div className="label-eyebrow text-rd-dim">Quantum state · recent verdicts</div>
        <span className="text-[10px] font-mono text-rd-dim">
          regime field + HOLD-lock signal per intent
        </span>
        <div className="ml-auto flex items-center gap-3 text-[10px] font-mono">
          <span className="text-rd-dim">
            count <span className="text-rd-text">{counters.total_returned ?? 0}</span>
          </span>
          {(counters.hold_locks ?? 0) > 0 && (
            <span className="text-rd-danger">
              ⚠ {counters.hold_locks} HOLD-LOCK{counters.hold_locks === 1 ? "" : "S"}
            </span>
          )}
          {(counters.with_notes ?? 0) > 0 && (
            <span className="text-rd-warn">{counters.with_notes} flagged</span>
          )}
        </div>
      </div>

      {err && (
        <div className="px-4 py-2 text-xs font-mono text-rd-danger border-b border-rd-border">
          {err}
        </div>
      )}

      {!items && <LoadingRow />}
      {items && items.length === 0 && (
        <div className="px-4 py-6 text-center text-rd-dim font-mono text-xs">
          no quantum verdicts yet — they appear after the next council evaluation
        </div>
      )}

      {items && items.length > 0 && (
        <div className="max-h-[500px] overflow-y-auto">
          <table className="w-full text-xs font-mono">
            <thead className="sticky top-0 bg-rd-bg3 text-rd-dim uppercase tracking-widest z-10">
              <tr>
                <th className="text-left px-3 py-2 border-b border-rd-border">When</th>
                <th className="text-left px-3 py-2 border-b border-rd-border">Symbol</th>
                <th className="text-left px-3 py-2 border-b border-rd-border">Lane</th>
                <th className="text-left px-3 py-2 border-b border-rd-border">Regime field</th>
                <th className="text-right px-3 py-2 border-b border-rd-border">Entropy</th>
                <th className="text-right px-3 py-2 border-b border-rd-border">Risk ×</th>
                <th className="text-left px-3 py-2 border-b border-rd-border">Notes</th>
              </tr>
            </thead>
            <tbody>
              {items.map((r, i) => {
                const rowKey = r.intent_id || r.id || `${r.ts || ""}-${r.symbol || ""}-${i}`;
                const probs = r.quantum.regime_probs || {};
                const top = Object.entries(probs).sort((a, b) => b[1] - a[1]).slice(0, 3);
                const isHoldLock = r.quantum.hold_lock_detected;
                return (
                  <tr
                    key={rowKey}
                    className="border-b border-rd-border hover:bg-rd-bg"
                    style={isHoldLock ? { background: "rgba(220,38,38,0.06)" } : undefined}
                    data-testid={`quantum-row-${i}`}
                  >
                    <td className="px-3 py-1.5 text-rd-dim whitespace-nowrap">
                      {r.ts ? relTime(r.ts) : "—"}
                    </td>
                    <td className="px-3 py-1.5 text-rd-text">{r.symbol || "—"}</td>
                    <td className="px-3 py-1.5">
                      <span className="text-[10px] uppercase text-rd-dim">{r.lane || "—"}</span>
                    </td>
                    <td className="px-3 py-1.5">
                      <div className="flex items-center gap-1">
                        {top.map(([regime, p]) => (
                          <span
                            key={regime}
                            className="inline-flex items-center gap-1 px-1.5 py-px text-[9px] uppercase"
                            style={{
                              color: REGIME_COLOR[regime] || "#A1A1AA",
                              border: `1px solid ${REGIME_COLOR[regime] || "#A1A1AA"}33`,
                            }}
                            title={`${regime}: ${(p * 100).toFixed(0)}%`}
                          >
                            <span
                              className="inline-block"
                              style={{
                                width: 4,
                                height: 8,
                                background: REGIME_COLOR[regime] || "#A1A1AA",
                                opacity: Math.max(0.3, p),
                              }}
                            />
                            {regime} {(p * 100).toFixed(0)}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="px-3 py-1.5 text-right text-rd-text">
                      {r.quantum.entropy?.toFixed(2) ?? "—"}
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      <span
                        style={{
                          color: r.quantum.risk_multiplier > 1.0 ? "#10B981" :
                                 r.quantum.risk_multiplier < 0.9 ? "#F59E0B" : "#E5E7EB",
                        }}
                      >
                        {r.quantum.risk_multiplier?.toFixed(3) ?? "—"}
                      </span>
                    </td>
                    <td className="px-3 py-1.5">
                      <div className="flex flex-wrap gap-1">
                        {isHoldLock && (
                          <Badge color="#DC2626">HOLD-LOCK</Badge>
                        )}
                        {(r.quantum.notes || []).filter((n) => n !== "HOLD_LOCK_DETECTED").map((n) => (
                          <Badge key={n} color="#F59E0B">{n.replace(/_/g, " ")}</Badge>
                        ))}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export default function Diagnostics() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  // Track when the last fetch SUCCEEDED so transient mobile network
  // blips don't wipe the screen. We only escalate to a big red
  // banner if the failure persists beyond a polling cycle or two.
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);

  const loadDiag = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/diagnostics");
      setData(data);
      setErr("");
      setLastSuccessAt(new Date());
      setConsecutiveFailures(0);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
      setConsecutiveFailures((n) => n + 1);
    }
  }, []);

  useEffect(() => { loadDiag(); }, [loadDiag]);
  // Refresh every 10s so the operator sees tier changes in near-real-time.
  useEffect(() => {
    const t = setInterval(loadDiag, 10000);
    return () => clearInterval(t);
  }, [loadDiag]);

  return (
    <div className="reveal" data-testid="diagnostics-page">
      <PageHeader
        eyebrow="Shared · Diagnostics"
        title="Health & liveness"
        sub="System health, MongoDB connectivity, and per-runtime liveness signals."
        testid="diagnostics-header"
      />

      <BrainDeepDiagnoseCard />

      {/* Post-deploy runtime validation suite (2026-02-26). Lives at
          the top so the FIRST thing the operator sees after a deploy
          is one-row truth: indexes present? auto-router ticking?
          sample query fast? brain emissions flowing? Replaces six
          hours of log-grepping with a single green/amber/red dot. */}
      <div className="mt-6">
        <HealthcheckTile />
      </div>

      <AdvisorPerformanceTile />

      <NativeBrainRuntimeTile />

      <BrainInputHealthTile />

      {/* ImposterScanCard removed 2026-02-21 — see import-block note. */}

      {/* 2026-02-19 (prod incident): when a polling fetch fails on
          mobile (network blip, backend slow under Webull-SDK load,
          etc.) we used to show a big red banner that hid everything
          else. Now we keep the last good `data` on screen and only
          flag the failure DISCRETELY in the header — and only after
          two consecutive failures, so a single dropped packet
          doesn't flash an alarm. The full-width red bar only fires
          when we've NEVER had data (first-load failure). */}
      {err && !data && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono" data-testid="diag-fatal-error">
          {err}
        </div>
      )}
      {err && data && consecutiveFailures >= 2 && (
        <div className="border border-rd-warn text-rd-warn px-3 py-1.5 mb-4 text-[11px] font-mono flex items-center justify-between" data-testid="diag-stale-warning">
          <span>
            data is stale · last successful refresh{" "}
            {lastSuccessAt ? `${Math.floor((Date.now() - lastSuccessAt.getTime()) / 1000)}s ago` : "never"}{" "}
            · {consecutiveFailures} consecutive refresh failures · retrying every 10s
          </span>
          <button
            onClick={loadDiag}
            className="ml-3 px-2 py-0.5 border border-rd-warn text-rd-warn hover:text-rd-text"
            data-testid="diag-retry-now"
          >
            retry now
          </button>
        </div>
      )}
      {!data && !err && <LoadingRow />}

      {data && (
        <>
          {/* Compressed header strip — replaces the old 3-card grid
              (Mongo / Mode / Now) with a single line. Saves vertical
              real estate; the 4 facts here are at-a-glance only. */}
          <div
            className="flex flex-wrap items-center gap-x-4 gap-y-1 mb-4 px-3 py-2 border border-rd-border bg-rd-bg2 font-mono text-[11px]"
            data-testid="diag-status-strip"
          >
            <span className="flex items-center gap-1.5" data-testid="diag-mongo">
              <span
                className={`inline-block w-2 h-2 ${data.mongo.ok ? "bg-rd-chevelle" : "bg-rd-danger"}`}
              />
              <span className="text-rd-dim uppercase tracking-wider">Mongo</span>
              <span className="text-rd-text font-bold">{data.mongo.ok ? "ONLINE" : "OFFLINE"}</span>
            </span>
            <span className="text-rd-border">·</span>
            <span className="flex items-center gap-1.5" data-testid="diag-mode">
              <span className="text-rd-dim uppercase tracking-wider">Mode</span>
              <span
                className="text-rd-text font-bold uppercase"
                style={{ color: data.lane_execution?.any_enabled ? "#10B981" : "#FBBF24" }}
              >
                {data.deploy_mode}
              </span>
            </span>
            {data.lane_execution && (
              <>
                <span className="text-rd-border">·</span>
                <span className="flex items-center gap-1.5">
                  <span className="text-rd-dim uppercase tracking-wider">Lanes</span>
                  <span
                    data-testid="diag-lane-equity-state"
                    style={{ color: data.lane_execution.equity ? "#10B981" : "#DC2626", fontWeight: 600 }}
                  >
                    EQ {data.lane_execution.equity ? "ON" : "OFF"}
                  </span>
                  <span
                    data-testid="diag-lane-crypto-state"
                    style={{ color: data.lane_execution.crypto ? "#10B981" : "#DC2626", fontWeight: 600 }}
                  >
                    CR {data.lane_execution.crypto ? "ON" : "OFF"}
                  </span>
                </span>
              </>
            )}
            <span className="text-rd-border">·</span>
            <span className="flex items-center gap-1.5" data-testid="diag-now">
              <span className="text-rd-dim uppercase tracking-wider">Server</span>
              <span className="text-rd-text">{fmtTime(data.now)}</span>
            </span>
            {data.mongo.error && (
              <span className="text-rd-danger ml-2">{data.mongo.error}</span>
            )}
          </div>

          {/* Legacy Runtimes table + CompositeLivenessCard dropped
              2026-02-19. The BrainHealthTile that briefly replaced
              them was itself removed 2026-07-06 as dead-on-arrival
              in prod. STALE HEARTBEAT alert below is preserved as
              the loudest liveness signal — any dead heartbeat needs
              operator eyes immediately. */}
          {data.runtimes.some((r) => r.heartbeat_tier === "dead") && (
            <div
              className="bg-rd-danger/15 border border-rd-danger px-4 py-2 mb-4 text-[11px] font-mono text-rd-danger"
              data-testid="stale-heartbeat-banner"
            >
              ⚠ STALE HEARTBEAT — {data.runtimes
                .filter((r) => r.heartbeat_tier === "dead")
                .map((r) => r.runtime.toUpperCase())
                .join(", ")}{" "}
              heartbeating ≥{data.heartbeat_preview_drift_seconds || 110}s ago. Possible hang, slow LLM call, or pod restart. For an actual MC-URL misconfig check, expand the <span className="text-rd-text font-bold">Sidecar identity check-ins</span> details below.
            </div>
          )}

          {/* Unified decisions feed — REMOVED 2026-07-06 (dead-on-arrival
              in prod). Operator alternatives: Intent Clearance Funnel
              (`/admin/intent-clearance-funnel`) and per-collection direct
              queries. */}

          {/* Live-trade diagnose — surfaces the EXACT gate blocking
              live execution on each lane. Built after the operator
              reported "no trades being made on crypto" — this panel
              makes the first blocker visible in one click. */}
          <div className="mt-6">
            <PanelErrorBoundary panelName="LaneExecutionTogglesPanel">
              <LaneExecutionTogglesPanel />
            </PanelErrorBoundary>
          </div>

          {/* Training-signal tile — bracket outcome distribution.
              Lazy-mounted (2026-02-19) — operator-rare deep-dive
              into per-confidence-band TP/SL/timeout calibration. */}
          <LazyDetails
            summary="Training signal · bracket outcomes (click to load)"
            testid="lazy-bracket-outcomes"
          >
            <PanelErrorBoundary panelName="BracketOutcomeDistributionPanel">
              <BracketOutcomeDistributionPanel />
            </PanelErrorBoundary>
          </LazyDetails>

          {/* Fingerprint diff — before/after doctrine-change validation.
              Reads session_fingerprints; aggregates two ranges into
              composites and surfaces the deltas (execution_ready_rate,
              gate_pass_rates, quality_dist, top_fail_reasons). Lazy-
              mounted because it's operator-triggered (post-deploy). */}
          <LazyDetails
            summary="Fingerprint diff · before / after doctrine change (click to load)"
            testid="lazy-fingerprint-diff"
          >
            <PanelErrorBoundary panelName="FingerprintDiffPanel">
              <FingerprintDiffPanel />
            </PanelErrorBoundary>
          </LazyDetails>

          {/* LiveTradeDiagnose removed 2026-07-01 — its `/admin/execution/diagnose`
              endpoint was deleted in the Pass 2/3 backend simplification.
              The Sidecar Trader's TradeTape (on Overview) now surfaces
              the actual lane-blocking reason per cycle. */}

          {/* Promotion artifact — REMOVED 2026-07-06 (dead-on-arrival
              in prod). Promotion decisions still land through Patent J
              countersign at `/admin/promotion/proposals`. */}

          {/* Brain-Health composite tile — REMOVED 2026-07-06
              (dead-on-arrival in prod). Post-redeploy sanity now goes
              through the 3 underlying endpoints directly: sidecar-checkin,
              opinion-silence-watchdog, and seat-walk. */}

          {/* Sidecar check-ins — Lazy-mounted (2026-02-19). Deep
              per-brain identity stamp inspection. The page-level
              imposter alert tile that used to sit above this was
              removed when the HTTP sidecar plumbing was retired
              (2026-02-21); this on-demand inspector remains for any
              residual in-process check-in trace work. */}
          <LazyDetails
            summary="Sidecar identity check-ins · stamps + verdicts (click to load)"
            testid="lazy-sidecar-checkin"
          >
            <PanelErrorBoundary panelName="SidecarCheckinPanel">
              <SidecarCheckinPanel />
            </PanelErrorBoundary>
          </LazyDetails>

          {/* RuntimeBundlesPanel + RuntimeTokensPanel moved to
              /admin/setup (2026-02-19) — operator-rare actions
              were burning two fetches per page load. Sidebar
              "Setup" link opens them. */}

          {/* Quantum-inspired state — Lazy-mounted (2026-02-19). */}
          <LazyDetails
            summary="Quantum overlay · recent verdicts (click to load)"
            testid="lazy-quantum"
          >
            <QuantumPanel />
          </LazyDetails>

          {/* VRL gate scorecards — Lazy-mounted (2026-02-19). Weekly
              review surface; not glance-level. */}
          <LazyDetails
            summary="VRL gate scorecards · precision/recall (click to load)"
            testid="lazy-vrl-scorecards"
          >
            <VRLScorecardsPanel />
          </LazyDetails>
        </>
      )}
    </div>
  );
}
