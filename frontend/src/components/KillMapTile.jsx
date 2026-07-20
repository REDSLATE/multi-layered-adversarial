import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";

/**
 * Kill Map — full-funnel throughput tile. Where do trades die?
 * Reads /api/admin/kill-map (Phase 1 of the passage-logic doctrine:
 * "block invalidity, not uncertainty" — measure before surgery).
 */
export default function KillMapTile() {
  const [data, setData] = useState(null);
  const [hours, setHours] = useState(24);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await api.get(`/admin/kill-map?hours=${hours}`);
      setData(r.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [hours]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const dies = data?.verdict?.startsWith("DIES");
  const flow = data?.verdict?.startsWith("FLOW");
  const verdictColor = dies ? "#EF4444" : flow ? "#10B981" : "#FBBF24";

  const p = data?.stage_1_pulse || {};
  const a = data?.stage_2_arbiter || {};
  const i = data?.stage_3_ingest || {};
  const b = data?.stage_5_broker || {};
  const feeders = data?.stage_0_feeders || {};
  const gs = i.by_gate_state || {};
  const blocked = (gs.blocked || 0) + (gs.rejected_at_ingest || 0) + (gs.advisory_only || 0);
  const executed = typeof b.executed_intents === "number" ? b.executed_intents : 0;
  const opinionsByBrain = Object.entries(p.opinions_by_brain || {}).filter(([k]) => !k.startsWith("_"));
  const opinionsTotal = opinionsByBrain.reduce((s, [, n]) => s + n, 0);
  const suppression = Object.entries(a.emission_suppression || {})
    .filter(([k]) => !k.startsWith("_") && k !== "emitted")
    .sort((x, y) => y[1] - x[1]);
  const submits = b.broker_submits || {};

  const stages = [
    { label: "SNAPS", value: p.snapshots_total },
    { label: "OPINIONS", value: opinionsTotal || undefined },
    { label: "ARBS", value: a.arbitrations },
    { label: "EMITTED", value: a.intents_emitted },
    { label: "CREATED", value: i.intents_created },
    { label: "BLOCKED", value: blocked, danger: blocked > 0 },
    { label: "SUBMITS", value: submits.attempts },
    { label: "EXECUTED", value: executed, success: executed > 0 },
  ];

  // First stage whose value drops to 0 after a non-zero predecessor —
  // that's where the funnel dies; paint it red. OPINIONS/SUBMITS may be
  // undefined on pre-instrumentation data; skip undefined stages.
  let deathIdx = -1;
  let prevVal = null;
  for (let k = 0; k < stages.length - 1; k++) {
    const v = stages[k].value;
    if (v === undefined || v === null) continue;
    if ((prevVal ?? 0) > 0 && v === 0) { deathIdx = k; break; }
    prevVal = v;
  }

  const reasons = (data?.stage_4_top_block_reasons || []).filter((r) => !r.error).slice(0, 5);

  return (
    <Card accentColor={verdictColor} className="mb-6" testid="kill-map-tile">
      <div className="flex items-start justify-between gap-4 flex-wrap mb-3">
        <div>
          <div className="label-eyebrow mb-1">Kill map · where trades die</div>
          <div className="text-[11px] font-mono text-rd-muted">
            pulse → arbiter → intent → gates → broker · block invalidity, not uncertainty
          </div>
        </div>
        <div className="flex items-center gap-1">
          {[24, 72, 168].map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              data-testid={`kill-map-window-${h}`}
              className={`px-2 py-0.5 text-[10px] font-mono border ${
                hours === h
                  ? "border-rd-text text-rd-text"
                  : "border-rd-border text-rd-dim hover:text-rd-text"
              }`}
            >
              {h === 24 ? "24H" : h === 72 ? "72H" : "7D"}
            </button>
          ))}
        </div>
      </div>

      {err && (
        <div className="text-[11px] font-mono text-rd-danger border border-rd-danger px-3 py-2 mb-3" data-testid="kill-map-error">
          {err}
        </div>
      )}

      {data && (
        <>
          <div
            className="text-[11px] font-mono px-3 py-2 mb-4 border"
            style={{ color: verdictColor, borderColor: `${verdictColor}55`, background: `${verdictColor}0d` }}
            data-testid="kill-map-verdict"
          >
            {data.verdict}
          </div>

          <div className="flex items-stretch gap-0 overflow-x-auto mb-4" data-testid="kill-map-funnel">
            {stages.map((s, k) => {
              const isDeath = k === deathIdx;
              const color = isDeath ? "#EF4444" : s.success && (s.value ?? 0) > 0 ? "#10B981" : s.danger ? "#F59E0B" : "#E5E7EB";
              return (
                <React.Fragment key={s.label}>
                  {k > 0 && (
                    <div className="flex items-center px-1.5 text-rd-dim text-xs font-mono shrink-0">→</div>
                  )}
                  <div
                    className="border border-rd-border px-3 py-2 min-w-[86px] shrink-0"
                    style={isDeath ? { borderColor: "#EF4444", background: "rgba(239,68,68,0.07)" } : undefined}
                    data-testid={`kill-map-stage-${s.label.toLowerCase()}`}
                  >
                    <div className="text-[9px] uppercase tracking-widest text-rd-dim">{s.label}</div>
                    <div className="font-display text-xl font-black tracking-tighter" style={{ color }}>
                      {s.value ?? "—"}
                    </div>
                  </div>
                </React.Fragment>
              );
            })}
          </div>

          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] font-mono text-rd-dim mb-1">
            <span data-testid="kill-map-pulses">{p.pulses ?? 0} pulses · avg {p.avg_snapshots_per_pulse ?? 0} snaps</span>
            {a.runtime_modes_seen && Object.entries(a.runtime_modes_seen).map(([m, n]) => (
              <Badge key={m} color={m === "LIVE" ? "#10B981" : "#FBBF24"} testid={`kill-map-mode-${m}`}>
                {m} · {n}
              </Badge>
            ))}
            {Object.entries(feeders).map(([prov, f]) => {
              const age = f?.age_s;
              const ok = typeof age === "number" && age < 900 && !f?.error_type;
              return (
                <span key={prov} className="flex items-center gap-1" data-testid={`kill-map-feeder-${prov}`}>
                  <span className="inline-block w-1.5 h-1.5" style={{ background: ok ? "#10B981" : "#EF4444" }} />
                  {prov} {typeof age === "number" ? `${Math.round(age)}s` : "?"}
                </span>
              );
            })}
          </div>

          {(opinionsByBrain.length > 0 || suppression.length > 0) && (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] font-mono text-rd-dim mb-1" data-testid="kill-map-dimensions">
              {opinionsByBrain.map(([brain, n]) => (
                <span key={brain} data-testid={`kill-map-opinions-${brain}`}>
                  {brain} <span className="text-rd-text">{n}</span>
                </span>
              ))}
              {suppression.map(([reason, n]) => (
                <span key={reason} className="text-rd-warn" data-testid={`kill-map-suppression-${reason}`}>
                  ⊘ {reason.replace(/_/g, " ")} <span className="font-bold">{n}</span>
                </span>
              ))}
            </div>
          )}

          {reasons.length > 0 && (
            <div className="mt-3 border border-rd-border" data-testid="kill-map-reasons">
              <div className="px-3 py-1.5 text-[9px] uppercase tracking-widest text-rd-dim border-b border-rd-border bg-rd-bg3">
                top block reasons
              </div>
              {reasons.map((r, k) => (
                <div
                  key={`${r.reason}-${r.lane}-${r.gate_state}`}
                  className="px-3 py-1.5 flex items-center gap-3 text-[11px] font-mono border-b border-rd-border last:border-b-0"
                  data-testid={`kill-map-reason-${k}`}
                >
                  <span className="text-rd-danger font-bold w-10 text-right shrink-0">{r.count}</span>
                  <span className="text-rd-text truncate" title={r.reason}>{r.reason}</span>
                  <span className="text-rd-dim ml-auto shrink-0">{r.lane} · {r.gate_state}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  );
}
