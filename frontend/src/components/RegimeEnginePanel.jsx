import React, { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { Card, Badge } from "@/components/ui-bits";
import { Waveform, ArrowsClockwise } from "@phosphor-icons/react";

const STATE_COLORS = ["#10B981", "#F59E0B", "#EF4444", "#3B82F6", "#A855F7", "#14B8A6"];

function ProbBar({ state, label, prob, delta, color }) {
  const pct = Math.round(prob * 100);
  const showDelta = delta !== null && delta !== undefined && Math.abs(delta) >= 0.01;
  return (
    <div className="mb-1.5" data-testid={`regime-state-bar-${state}`}>
      <div className="flex items-center justify-between text-xs mb-0.5">
        <span className="text-zinc-400">
          <span className="font-mono text-zinc-500 mr-1">S{state}</span>
          {label}
        </span>
        <span className="font-mono">
          {pct}%
          {showDelta && (
            <span className={delta > 0 ? "text-emerald-400 ml-1" : "text-red-400 ml-1"}>
              {delta > 0 ? "▲" : "▼"}{Math.abs(Math.round(delta * 100))}
            </span>
          )}
        </span>
      </div>
      <div className="h-1.5 rounded bg-zinc-800 overflow-hidden">
        <div className="h-full rounded transition-[width] duration-500"
          style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

function TimelineRibbon({ lane, timeline }) {
  if (!timeline || timeline.length === 0) return null;
  return (
    <div className="mt-2" data-testid={`regime-timeline-${lane}`}>
      <div className="text-[10px] uppercase tracking-wide text-zinc-600 mb-1">
        {timeline.length}-day state history
      </div>
      <div className="flex h-3 rounded overflow-hidden">
        {timeline.map((d) => (
          <div key={d.date} className="flex-1 min-w-[2px]"
            style={{ background: STATE_COLORS[d.state % STATE_COLORS.length],
                     opacity: 0.4 + 0.6 * (d.prob || 1) }}
            title={`${d.date} · S${d.state} ${d.label} (${Math.round((d.prob || 0) * 100)}%)`} />
        ))}
      </div>
      <div className="flex justify-between text-[9px] font-mono text-zinc-600 mt-0.5">
        <span>{timeline[0]?.date}</span>
        <span>{timeline[timeline.length - 1]?.date}</span>
      </div>
    </div>
  );
}

function LaneCard({ lane, snap }) {
  if (!snap) {
    return (
      <div className="flex-1 min-w-[260px] rounded-lg border border-zinc-800 p-3"
        data-testid={`regime-lane-${lane}`}>
        <div className="text-sm font-semibold uppercase tracking-wide text-zinc-400 mb-2">{lane}</div>
        <div className="text-xs text-zinc-500">Warming up — no snapshot yet.</div>
      </div>
    );
  }
  const transitioning = (snap.deltas || []).some((d) => Math.abs(d) >= 0.15);
  const agree = snap.agreement || {};
  const overlapPct = Math.round((agree.overlap || 0) * 100);
  return (
    <div className="flex-1 min-w-[260px] rounded-lg border border-zinc-800 p-3"
      data-testid={`regime-lane-${lane}`}>
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-semibold uppercase tracking-wide text-zinc-400">{lane}</div>
        <span className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
          style={{
            background: `${STATE_COLORS[snap.top_state % STATE_COLORS.length]}22`,
            color: STATE_COLORS[snap.top_state % STATE_COLORS.length],
            border: `1px solid ${STATE_COLORS[snap.top_state % STATE_COLORS.length]}66`,
          }}
          data-testid={`regime-top-label-${lane}`}>
          {snap.top_label} · {Math.round(snap.top_prob * 100)}%
        </span>
      </div>
      {(snap.states || []).map((s) => (
        <ProbBar key={s.state} state={s.state} label={s.label} prob={s.prob}
          delta={snap.deltas ? snap.deltas[s.state] : null}
          color={STATE_COLORS[s.state % STATE_COLORS.length]} />
      ))}
      {transitioning && (
        <div className="mt-2 text-xs text-amber-400" data-testid={`regime-transition-alert-${lane}`}>
          ⚠ State shift developing — probability mass moving between regimes
        </div>
      )}
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        <Badge color={agree.argmax_match ? "#10B981" : "#F59E0B"}
          testid={`regime-agreement-${lane}`}>
          HMM≈GMM {overlapPct}%
        </Badge>
        <Badge color="#71717A" testid={`regime-entropy-${lane}`}>
          uncertainty {Math.round((snap.entropy || 0) * 100)}%
        </Badge>
      </div>
      <div className="mt-2 text-[10px] font-mono text-zinc-600"
        data-testid={`regime-model-version-${lane}`}>
        {snap.model_version} · asof {snap.feature_asof}
      </div>
      <TimelineRibbon lane={lane} timeline={snap.timeline} />
    </div>
  );
}

function BrainMatrix({ matrix, edge }) {
  if (!matrix) return null;
  const lanes = Object.entries(matrix.lanes || {});
  if (lanes.length === 0) {
    return (
      <div className="text-xs text-zinc-500 mt-3" data-testid="regime-matrix-empty">
        No regime-stamped outcomes resolved yet — the matrix fills as the
        Outcome Engine scores stamped intents (cluster-adjusted).
      </div>
    );
  }
  return (
    <div className="mt-3 space-y-3" data-testid="regime-brain-matrix">
      {lanes.map(([lane, data]) => {
        const stateIds = Object.keys(data.state_labels || {}).sort((a, b) => a - b);
        const edgeBrains = edge?.lanes?.[lane]?.brains || {};
        return (
          <div key={lane}>
            <div className="text-xs uppercase tracking-wide text-zinc-500 mb-1">
              {lane} — brain × regime edge (prob-weighted, cluster-adjusted)
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-zinc-500">
                    <th className="text-left py-1 pr-2">brain</th>
                    {stateIds.map((s) => (
                      <th key={s} className="text-right py-1 px-2 font-mono">
                        S{s} {data.state_labels[s]}
                      </th>
                    ))}
                    <th className="text-right py-1 px-2">n_eff / raw</th>
                    <th className="text-right py-1 pl-2">shadow ×</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(data.brains).map(([brain, row]) => {
                    const er = edgeBrains[brain];
                    return (
                      <tr key={brain} className="border-t border-zinc-800"
                        data-testid={`regime-matrix-row-${lane}-${brain}`}>
                        <td className="py-1 pr-2 text-zinc-300">{brain}</td>
                        {stateIds.map((s) => {
                          const cell = row.states[s];
                          const edgePct = cell?.edge_pct;
                          return (
                            <td key={s} className="text-right py-1 px-2 font-mono">
                              {edgePct === null || edgePct === undefined ? "—" : (
                                <span className={edgePct >= 0 ? "text-emerald-400" : "text-red-400"}>
                                  {(edgePct * 100).toFixed(2)}%
                                </span>
                              )}
                              <span className="text-zinc-600 ml-1">({cell?.eff_n ?? 0})</span>
                            </td>
                          );
                        })}
                        <td className="text-right py-1 px-2 font-mono text-zinc-500">
                          {row.n_eff ?? "—"} / {row.n_raw ?? row.n ?? "—"}
                        </td>
                        <td className="text-right py-1 pl-2 font-mono"
                          data-testid={`regime-shadow-mult-${lane}-${brain}`}
                          title={er?.reason || ""}>
                          {er ? (
                            <span className={er.multiplier > 1 ? "text-emerald-400"
                              : er.multiplier < 1 ? "text-amber-400" : "text-zinc-400"}>
                              {er.multiplier?.toFixed(2)}×
                            </span>
                          ) : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function RegimeEnginePanel() {
  const [state, setState] = useState(null);
  const [matrix, setMatrix] = useState(null);
  const [edge, setEdge] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/regime/state");
      setState(data);
    } catch { /* panel is advisory — stay quiet */ }
    try {
      const { data } = await api.get("/admin/regime/brain-matrix");
      setMatrix(data);
    } catch { /* ignore */ }
    try {
      const { data } = await api.get("/admin/regime/edge-preview");
      setEdge(data);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  const refresh = async () => {
    setBusy(true);
    try {
      await api.post("/admin/regime/refresh", {});
      toast.success("Regime snapshot refreshed");
      await load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Refresh failed");
    } finally {
      setBusy(false);
    }
  };

  const lanes = state?.lanes || {};
  return (
    <Card className="mb-6" testid="regime-engine-panel" accentColor="#3B82F6">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Waveform size={16} weight="fill" color="#3B82F6" />
          <h3 className="font-display text-lg font-bold tracking-tight">Regime Engine</h3>
          <Badge color="#3B82F6" testid="regime-advisory-badge">ADVISORY — no sizing impact</Badge>
          <Badge color={edge?.armed ? "#EF4444" : "#71717A"} testid="regime-edge-armed-badge">
            {edge?.armed ? "EDGE ARMED" : "EDGE SHADOW"}
          </Badge>
          <Badge color={state?.worker?.running ? "#10B981" : "#71717A"}
            testid="regime-worker-status">
            {state?.worker?.running ? "worker live" : "worker off"}
          </Badge>
        </div>
        <button onClick={refresh} disabled={busy}
          className="flex items-center gap-1 text-xs text-zinc-400 hover:text-zinc-200 disabled:opacity-50"
          data-testid="regime-refresh-btn">
          <ArrowsClockwise size={14} className={busy ? "animate-spin" : ""} />
          refresh
        </button>
      </div>
      <div className="flex gap-3 flex-wrap">
        <LaneCard lane="equity" snap={lanes.equity} />
        <LaneCard lane="crypto" snap={lanes.crypto} />
      </div>
      <BrainMatrix matrix={matrix} edge={edge} />
    </Card>
  );
}
