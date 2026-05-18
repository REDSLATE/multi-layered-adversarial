import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";
import VRLScorecardsPanel from "@/components/VRLScorecardsPanel";
import LiveTradeDiagnose from "@/components/LiveTradeDiagnose";
import RuntimeTokensPanel from "@/components/RuntimeTokensPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";

const BRAINS_FOR_FILTER = ["all", "alpha", "camaro", "chevelle", "redeye"];
const KIND_LABEL = {
  receipt: "RECEIPT",
  sovereign_audit: "SOV-AUDIT",
  intent: "INTENT",
  training_signal: "TRAINING",
};
const KIND_COLOR = {
  receipt: "#10B981",
  sovereign_audit: "#DC2626",
  intent: "#3B82F6",
  training_signal: "#F59E0B",
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

  const emptyPayloadCount = useMemo(
    () => (items || []).filter((r) => (r.summary || "").includes("empty payload")).length,
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
                const isSkeleton = (r.summary || "").includes("empty payload");
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

  const loadDiag = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/diagnostics");
      setData(data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
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

      {err && <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">{err}</div>}
      {!data && <LoadingRow />}

      {data && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6 mb-6">
            <Card testid="diag-mongo">
              <div className="label-eyebrow mb-2">MongoDB</div>
              <div className="flex items-center gap-2">
                <span
                  className={`inline-block w-2 h-2 ${data.mongo.ok ? "bg-rd-chevelle pulse-dot" : "bg-rd-danger"}`}
                />
                <span className="font-display text-2xl font-bold tracking-tight">
                  {data.mongo.ok ? "ONLINE" : "OFFLINE"}
                </span>
              </div>
              {data.mongo.error && (
                <div className="text-xs font-mono text-rd-danger mt-2">{data.mongo.error}</div>
              )}
            </Card>
            <Card testid="diag-mode">
              <div className="label-eyebrow mb-2">Deploy mode</div>
              <div className="font-display text-2xl font-bold tracking-tight uppercase" style={{ color: data.deploy_mode === "execution" ? "#10B981" : "#FBBF24" }}>
                {data.deploy_mode}
              </div>
              <div className="text-xs text-rd-muted mt-2 font-mono">
                {data.deploy_mode === "execution" ? "live order routing enabled" : "no broker has execution_enabled=true"}
              </div>
            </Card>
            <Card testid="diag-now">
              <div className="label-eyebrow mb-2">Server time</div>
              <div className="font-mono text-sm text-rd-text">{fmtTime(data.now)}</div>
            </Card>
          </div>

          <Card className="p-0 overflow-hidden" testid="diag-runtimes">
            {/* Top-of-table banner — fires the moment any brain crosses
                the 110s preview-drift threshold. Operator rule:
                ≥110s = brain is likely pointed at the PREVIEW URL,
                not prod. Catches Camaro-style config drift fast. */}
            {data.runtimes.some((r) => r.heartbeat_tier === "preview_drift") && (
              <div
                className="bg-rd-danger/15 border-b border-rd-danger px-4 py-2 text-[11px] font-mono text-rd-danger"
                data-testid="preview-drift-banner"
              >
                ⚠ PREVIEW DRIFT — {data.runtimes
                  .filter((r) => r.heartbeat_tier === "preview_drift")
                  .map((r) => r.runtime.toUpperCase())
                  .join(", ")}{" "}
                heartbeating ≥{data.heartbeat_preview_drift_seconds || 110}s ago. Likely pointed at the preview URL, not{" "}
                <span className="text-rd-text font-bold">mission.risedual.ai</span>. Check the sidecar's <code>MC_BASE_URL</code>.
              </div>
            )}
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                  <th className="text-left px-4 py-3 border-b border-rd-border">Runtime</th>
                  <th className="text-right px-4 py-3 border-b border-rd-border">Decision log</th>
                  <th className="text-right px-4 py-3 border-b border-rd-border">Memory labels</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Last receipt</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.runtimes.map((r) => {
                  const meta = RUNTIME_META[r.runtime];
                  // Three-tier operator doctrine. Tier comes from backend
                  // so frontend & backend can never disagree on what counts
                  // as drift vs preview-drift.
                  const tier = r.heartbeat_tier || (r.heartbeat_stale ? "preview_drift" : "ok");
                  const tierMeta = {
                    ok:            { color: "#10B981", label: "LIVE" },
                    drift:         { color: "#F59E0B", label: "DRIFT" },
                    preview_drift: { color: "#DC2626", label: "PREVIEW URL" },
                    unknown:       { color: "#A1A1AA", label: "NO HEARTBEAT" },
                  }[tier];
                  return (
                    <tr
                      key={r.runtime}
                      className="border-b border-rd-border last:border-b-0"
                      data-testid={`diag-row-${r.runtime}`}
                    >
                      <td className="px-4 py-2.5">
                        <span style={{ color: meta.color }} className="font-bold">
                          {meta.label}
                        </span>
                        <span className="text-rd-dim ml-2">· {meta.project}</span>
                      </td>
                      <td className="px-4 py-2.5 text-right">{r.log_count}</td>
                      <td className="px-4 py-2.5 text-right">{r.memory_labels_count}</td>
                      <td className="px-4 py-2.5">
                        {r.last_receipt_ts ? `${fmtTime(r.last_receipt_ts)} (${relTime(r.last_receipt_ts)})` : "—"}
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge color={tierMeta.color}>{tierMeta.label}</Badge>
                        {r.heartbeat_age_seconds != null && (
                          <span
                            className="ml-2"
                            style={{ color: tierMeta.color }}
                            data-testid={`diag-hb-age-${r.runtime}`}
                          >
                            {Math.floor(r.heartbeat_age_seconds)}s
                          </span>
                        )}
                        {tier === "preview_drift" && (
                          <span
                            className="ml-2 text-[10px] text-rd-danger"
                            title="≥110s — operator rule says brain is on preview URL"
                          >
                            · likely on preview URL
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>

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
            <PanelErrorBoundary panelName="LiveTradeDiagnose">
              <LiveTradeDiagnose />
            </PanelErrorBoundary>
          </div>

          {/* Brain ingest tokens — read-back of the per-runtime
              X-Runtime-Token MC expects. One-click reveal + copy +
              .env snippet download per brain. Built so the operator
              can compare each brain's MONOREPO_INGEST_TOKEN against
              MC's <BRAIN>_INGEST_TOKEN env var. */}
          <div className="mt-6">
            <PanelErrorBoundary panelName="RuntimeTokensPanel">
              <RuntimeTokensPanel />
            </PanelErrorBoundary>
          </div>

          {/* Quantum-inspired state — regime probability field +
              HOLD-lock detection per recent intent. Surfaces the
              quantum overlay verdict from the governance ledger. */}
          <div className="mt-6">
            <QuantumPanel />
          </div>

          {/* VRL gate scorecards — per-gate precision/recall over a
              rolling window. Shows which gates are net helpful vs.
              net friction so the operator can retune. */}
          <div className="mt-6">
            <VRLScorecardsPanel />
          </div>
        </>
      )}
    </div>
  );
}
