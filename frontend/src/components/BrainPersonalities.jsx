import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge, EmptyState } from "@/components/ui-bits";
import { ArrowsClockwise, Brain } from "@phosphor-icons/react";

const BRAIN_COLOR = {
  camino: "#3B82F6",
  barracuda: "#EF4444",
  hellcat: "#F59E0B",
  gto: "#10B981",
};

/**
 * Brain Personalities — combines D (dissent) + E (accuracy) into
 * one compact operator readout. Each row is a brain's independent
 * track record: how often it fires as executor, its fill rate, avg
 * confidence, and how often it dissents when it's NOT the executor.
 *
 * Philosophy: statistics are per-brain, never averaged across.
 * Preserves the specialist identity — a brain earns its own history.
 */
export default function BrainPersonalities() {
  const [accuracy, setAccuracy] = useState(null);
  const [dissent, setDissent] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [windowHrs, setWindowHrs] = useState(24);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [a, d] = await Promise.all([
        api.get("/admin/trader/brain-accuracy", { params: { window_hours: windowHrs } }),
        api.get("/admin/trader/dissent", { params: { window_hours: windowHrs } }),
      ]);
      setAccuracy(a.data);
      setDissent(d.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  }, [windowHrs]);

  useEffect(() => { load(); }, [load]);

  // Merge the two datasets keyed by brain
  const byBrain = new Map();
  (accuracy?.brains || []).forEach((r) => byBrain.set(r.brain, { accuracy: r }));
  (dissent?.brains || []).forEach((r) => {
    const cur = byBrain.get(r.brain) || {};
    byBrain.set(r.brain, { ...cur, dissent: r });
  });
  const rows = Array.from(byBrain.entries()).map(([brain, v]) => ({ brain, ...v }));
  rows.sort((a, b) => (b.accuracy?.fires || 0) - (a.accuracy?.fires || 0));

  return (
    <Card className="mb-6" testid="brain-personalities-tile">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 text-rd-dim">
            <Brain size={16} weight="duotone" />
          </div>
          <div>
            <div className="font-display text-base font-bold text-rd-text leading-none">
              Brain Personalities
            </div>
            <div className="text-[11px] text-rd-muted mt-1 font-mono leading-relaxed">
              Per-brain track records over the last {windowHrs}h. Statistics stay separate — never averaged.
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={windowHrs}
            onChange={(e) => setWindowHrs(Number(e.target.value))}
            className="text-[10px] uppercase tracking-widest px-2 py-1 border border-rd-border bg-transparent text-rd-text font-mono"
            data-testid="brain-personalities-window"
          >
            <option value={1}>1h</option>
            <option value={4}>4h</option>
            <option value={24}>24h</option>
            <option value={168}>7d</option>
          </select>
          <button
            onClick={load}
            disabled={busy}
            className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
            title="Reload"
            data-testid="brain-personalities-reload"
          >
            <ArrowsClockwise size={12} weight="bold" className={busy ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-3 text-xs font-mono" data-testid="brain-personalities-error">
          {err}
        </div>
      )}

      {rows.length === 0 ? (
        <EmptyState
          message="No fires or signals in this window. Track records accumulate as the trader runs."
          testid="brain-personalities-empty"
        />
      ) : (
        <div className="border border-rd-border divide-y divide-rd-border" data-testid="brain-personalities-rows">
          <div className="grid grid-cols-12 gap-2 px-2 py-1.5 bg-rd-bg text-[9px] uppercase tracking-widest text-rd-dim font-mono">
            <div className="col-span-2">brain</div>
            <div className="col-span-1 text-right">fires</div>
            <div className="col-span-2 text-right">fill rate</div>
            <div className="col-span-2 text-right">avg conf</div>
            <div className="col-span-2 text-right">avg spread @ fire</div>
            <div className="col-span-1 text-right">dissent</div>
            <div className="col-span-2">top dissents vs</div>
          </div>
          {rows.map((r) => {
            const brain = r.brain;
            const color = BRAIN_COLOR[brain] || "#A1A1AA";
            const a = r.accuracy || {};
            const d = r.dissent || {};
            const topVs = Object.entries(d.top_dissents_vs || {})
              .slice(0, 3)
              .map(([n, c]) => `${n}(${c})`)
              .join(" ");
            return (
              <div
                key={brain}
                className="grid grid-cols-12 gap-2 px-2 py-1.5 items-center text-[11px] font-mono"
                data-testid={`brain-personalities-row-${brain}`}
              >
                <div className="col-span-2 uppercase tracking-widest text-[10px] font-bold" style={{ color }}>
                  {brain}
                </div>
                <div className="col-span-1 text-right text-rd-text">
                  {a.fires || 0}
                </div>
                <div className="col-span-2 text-right">
                  {a.fires ? (
                    <span className={a.fill_rate_pct >= 90 ? "text-rd-text" : a.fill_rate_pct >= 60 ? "text-rd-warn" : "text-rd-danger"}>
                      {a.fill_rate_pct}% ({a.fills}/{a.fires})
                    </span>
                  ) : (
                    <span className="text-rd-muted">—</span>
                  )}
                </div>
                <div className="col-span-2 text-right text-rd-muted">
                  {a.avg_confidence != null ? a.avg_confidence.toFixed(3) : "—"}
                </div>
                <div className="col-span-2 text-right text-rd-muted">
                  {a.avg_spread_bps_at_fire != null ? `${a.avg_spread_bps_at_fire.toFixed(2)} bps` : "—"}
                </div>
                <div className="col-span-1 text-right">
                  {d.cycles ? (
                    <Badge color={d.dissent_rate_pct > 50 ? "#F59E0B" : "#71717A"}>
                      {d.dissent_rate_pct}%
                    </Badge>
                  ) : (
                    <span className="text-rd-muted">—</span>
                  )}
                </div>
                <div className="col-span-2 text-rd-muted truncate" title={topVs}>
                  {topVs || "—"}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
