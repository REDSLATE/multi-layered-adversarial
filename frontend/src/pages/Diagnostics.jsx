import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";
import VRLScorecardsPanel from "@/components/VRLScorecardsPanel";
import LiveTradeDiagnose from "@/components/LiveTradeDiagnose";
// RuntimeTokensPanel + RuntimeBundlesPanel moved to /admin/setup (2026-02-19)
//   — operator-rare actions; no longer mounted on Diagnostics.
import SidecarCheckinPanel from "@/components/SidecarCheckinPanel";
import BrainHealthTile from "@/components/BrainHealthTile";
import LaneExecutionTogglesPanel from "@/components/LaneExecutionTogglesPanel";
import BracketOutcomeDistributionPanel from "@/components/BracketOutcomeDistributionPanel";
import PromotionArtifactPanel from "@/components/PromotionArtifactPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import BrainDeepDiagnoseCard from "@/components/BrainDeepDiagnoseCard";
import ImposterScanCard from "@/components/ImposterScanCard";
// CompositeLivenessCard + the legacy runtimes table dropped (2026-02-19)
//   — BrainHealthTile is the modern composite that absorbs both.

const BRAINS_FOR_FILTER = ["all", "alpha", "camaro", "chevelle", "redeye"];

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
const KIND_LABEL = {
  receipt: "RECEIPT",
  sovereign_audit: "SOV-AUDIT",
  intent: "INTENT",
  engine_audit: "ENGINE",
  // Back-compat: any cached rows with the legacy label still render.
  training_signal: "ENGINE",
};
const KIND_COLOR = {
  receipt: "#10B981",
  sovereign_audit: "#DC2626",
  intent: "#3B82F6",
  engine_audit: "#64748B",
  training_signal: "#64748B",
};

function DecisionsFeed() {
  const [items, setItems] = useState(null);
  const [counts, setCounts] = useState({});
  const [brain, setBrain] = useState("all");
  const [err, setErr] = useState("");
  const [expanded, setExpanded] = useState(null);

  const load = useCallback(async () => {
    try {
      const params = { limit: 60 };
      if (brain !== "all") params.brain = brain;
      const { data } = await api.get("/admin/decisions", { params });
      setItems(data?.items || []);
      setCounts(data?.counts_per_source || {});
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [brain]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(load, 12000);
    return () => clearInterval(t);
  }, [load]);

  // "Pre-gate row" = a contribution audit row that has no substance.
  // (The 2026-05-24 empty-contribution gate now blocks these at ingest,
  // so this count should trend to zero for new traffic. Historical rows
  // can still match.) The "(empty payload)" branch covers non-
  // contribution sovereign rows that legitimately carry no payload.
  const emptyPayloadCount = useMemo(
    () => (items || []).filter((r) => {
      const s = r.summary || "";
      return s.includes("(no substance") || s.includes("(empty payload)");
    }).length,
    [items],
  );

  return (
    <Card className="p-0 overflow-hidden" testid="decisions-feed">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3 border-b border-rd-border bg-rd-bg3">
        <div className="label-eyebrow text-rd-dim">Decisions feed</div>
        <span className="text-[10px] font-mono text-rd-dim">
          unified across receipts · sovereign-audit · intents · training rows
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          {BRAINS_FOR_FILTER.map((b) => {
            const active = b === brain;
            const meta = b === "all" ? null : RUNTIME_META[b];
            return (
              <button
                key={b}
                onClick={() => setBrain(b)}
                data-testid={`decisions-filter-${b}`}
                className={
                  "px-2 py-1 text-[10px] font-mono uppercase tracking-wider border " +
                  (active
                    ? "border-rd-text text-rd-text bg-rd-bg"
                    : "border-rd-border text-rd-dim hover:text-rd-text")
                }
                style={active && meta ? { borderColor: meta.color, color: meta.color } : undefined}
              >
                {b}
              </button>
            );
          })}
        </div>
      </div>

      {err && (
        <div className="px-4 py-2 text-xs font-mono text-rd-danger border-b border-rd-border">
          {err}
        </div>
      )}

      {/* Per-collection counts strip — surfaces which stores the brain
          actually writes to. Critical for diagnosing REDEYE-style
          "decisions exist but in a different collection" issues. */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 px-4 py-2 bg-rd-bg text-[10px] font-mono text-rd-dim border-b border-rd-border">
        {Object.entries(counts).map(([coll, n]) => (
          <span key={coll}>
            <span className="text-rd-text">{coll}</span>: {n}
          </span>
        ))}
        {emptyPayloadCount > 0 && (
          <span className="ml-auto text-rd-danger">
            ⚠ {emptyPayloadCount} skeleton row{emptyPayloadCount === 1 ? "" : "s"} (empty payload — engine not emitting substance)
          </span>
        )}
      </div>

      {!items && <LoadingRow />}
      {items && items.length === 0 && (
        <div className="px-4 py-6 text-center text-rd-dim font-mono text-xs">
          no decisions captured for this filter
        </div>
      )}

      {items && items.length > 0 && (
        <div className="max-h-[500px] overflow-y-auto">
          <table className="w-full text-xs font-mono">
            <thead className="sticky top-0 bg-rd-bg3 text-rd-dim uppercase tracking-widest z-10">
              <tr>
                <th className="text-left px-3 py-2 border-b border-rd-border">When</th>
                <th className="text-left px-3 py-2 border-b border-rd-border">Brain</th>
                <th className="text-left px-3 py-2 border-b border-rd-border">Kind</th>
                <th className="text-left px-3 py-2 border-b border-rd-border">Summary</th>
              </tr>
            </thead>
            <tbody>
              {items.map((r, i) => {
                const rowKey = r.id || `${r.ts}-${r.source || r.brain || "x"}-${i}`;
                const meta = r.brain && RUNTIME_META[r.brain];
                const isOpen = expanded === rowKey;
                const isSkeleton = (r.summary || "").includes("(no substance") || (r.summary || "").includes("(empty payload)");
                return (
                  <React.Fragment key={rowKey}>
                    <tr
                      className="border-b border-rd-border hover:bg-rd-bg cursor-pointer"
                      onClick={() => setExpanded(isOpen ? null : rowKey)}
                      data-testid={`decisions-row-${i}`}
                    >
                      <td className="px-3 py-1.5 text-rd-dim whitespace-nowrap">
                        {r.ts ? relTime(r.ts) : "—"}
                      </td>
                      <td className="px-3 py-1.5 whitespace-nowrap">
                        {meta ? (
                          <span style={{ color: meta.color }} className="font-bold">
                            {meta.label}
                          </span>
                        ) : (
                          <span className="text-rd-dim">{r.brain || "—"}</span>
                        )}
                      </td>
                      <td className="px-3 py-1.5">
                        <Badge color={KIND_COLOR[r.kind] || "#A1A1AA"}>
                          {KIND_LABEL[r.kind] || r.kind}
                        </Badge>
                      </td>
                      <td
                        className="px-3 py-1.5 text-rd-text"
                        style={isSkeleton ? { color: "#F59E0B" } : undefined}
                      >
                        {r.summary || "—"}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="bg-rd-bg">
                        <td colSpan={4} className="px-3 py-3">
                          <div className="text-[10px] text-rd-dim mb-1.5">
                            source: <span className="text-rd-text">{r.source_collection}</span>
                          </div>
                          <pre className="text-[10px] text-rd-text bg-rd-bg2 border border-rd-border p-2 overflow-x-auto leading-snug">
                            {JSON.stringify(r.raw, null, 2)}
                          </pre>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
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

      <ImposterScanCard />

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
              2026-02-19 — BrainHealthTile is the modern single-glance
              composite that absorbs both. STALE HEARTBEAT alert is
              now surfaced inline by BrainHealthTile's regression
              detector. */}

          {/* STALE HEARTBEAT alert preserved as a thin banner even
              though the full legacy Runtimes table was dropped
              2026-02-19 (BrainHealthTile absorbs the per-brain
              freshness signal more clearly). This alert is the
              loudest signal — any dead heartbeat needs operator
              eyes immediately. */}
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

          {/* Unified decisions feed — surfaces every brain's output
              regardless of which collection the engine wrote it to.
              REDEYE's contributions, Chevelle's authority_calls, Camaro
              intents, and MC training rows all appear here. */}
          <div className="mt-6">
            <DecisionsFeed />
          </div>

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

          <div className="mt-6">
            <PanelErrorBoundary panelName="LiveTradeDiagnose">
              <LiveTradeDiagnose />
            </PanelErrorBoundary>
          </div>

          {/* Promotion artifact — shadow-proposal vs alpha-fill evidence.
              Operators read this to decide whether a challenger brain
              has earned promotion to an executor seat. Verdicts here
              are advisory; the Patent-J countersign at
              /admin/promotion/proposals is still the only path to flip
              authority. */}
          <div className="mt-6">
            <PanelErrorBoundary panelName="PromotionArtifactPanel">
              <PromotionArtifactPanel />
            </PanelErrorBoundary>
          </div>

          {/* Brain-Health composite tile — single-glance fleet
              readiness. Joins sidecar-checkin + opinion-watchdog +
              data-keys-audit + sovereign-audit-log per (role, lane).
              Built so post-redeploy verification collapses to one
              page glance instead of three curls. Read-only. */}
          <div className="mt-6">
            <PanelErrorBoundary panelName="BrainHealthTile">
              <BrainHealthTile />
            </PanelErrorBoundary>
          </div>

          {/* Sidecar check-ins — Lazy-mounted (2026-02-19). Deep
              per-brain identity stamp inspection; ImposterScanCard
              at the top of the page is the alert-level summary,
              this is the on-demand inspector. */}
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
