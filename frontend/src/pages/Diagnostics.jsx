import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";

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
                const meta = r.brain && RUNTIME_META[r.brain];
                const isOpen = expanded === i;
                const isSkeleton = (r.summary || "").includes("empty payload");
                return (
                  <React.Fragment key={i}>
                    <tr
                      className="border-b border-rd-border hover:bg-rd-bg cursor-pointer"
                      onClick={() => setExpanded(isOpen ? null : i)}
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
              <div className="font-display text-2xl font-bold tracking-tight text-rd-warn uppercase">
                {data.deploy_mode}
              </div>
              <div className="text-xs text-rd-muted mt-2 font-mono">execution disabled</div>
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
        </>
      )}
    </div>
  );
}
