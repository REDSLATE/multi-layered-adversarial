import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { ArrowsClockwise } from "@phosphor-icons/react";

const COUNTER_LABELS = [
  ["broker_fills_total", "broker fills"],
  ["recorded_fills_total", "recorded"],
  ["unlinked_fills", "unlinked"],
  ["paired_round_trips", "round trips"],
  ["measured_cost_count", "measured-cost"],
  ["exit_linkage_miss_count", "orphan exits"],
];

/** Broker-fill reconciliation — broker truth vs internal records. */
export const ReconciliationPanel = () => {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [orphans, setOrphans] = useState(null);
  const [scorecard, setScorecard] = useState(null);
  const [resolveForm, setResolveForm] = useState({});

  const load = () => {
    api.get("/admin/reconciliation").then(({ data: d }) => setData(d)).catch(() => {});
    api.get("/admin/reconciliation/repeat-scorecard").then(({ data: d }) => setScorecard(d)).catch(() => {});
  };
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, []);

  const loadOrphans = async () => {
    try {
      const { data: d } = await api.get("/admin/reconciliation/orphans");
      setOrphans(d);
    } catch (e) {
      toast.error(String(e?.response?.data?.detail || e.message));
    }
  };

  const resolveOrphan = async (fillId) => {
    const form = resolveForm[fillId] || {};
    try {
      const { data: d } = await api.post("/admin/reconciliation/orphans/resolve", {
        fill_id: fillId,
        entry_price: parseFloat(form.price),
        note: form.note || "",
        position_id: form.position_id || null,
      });
      toast.success(`orphan resolved — ${d.outcome.symbol} realized ${d.outcome.realized_pnl_usd >= 0 ? "+" : ""}$${d.outcome.realized_pnl_usd.toFixed(4)}`);
      loadOrphans();
      load();
    } catch (e) {
      const det = e?.response?.data?.detail;
      toast.error(typeof det === "string" ? det : JSON.stringify(det || e.message));
    }
  };

  const run = async (full) => {
    setBusy(true);
    try {
      const { data: d } = await api.post("/admin/reconciliation/run", { full });
      const c = d.report?.counters || {};
      toast.success(`reconciliation ${full ? "backfill" : "run"} done — ${c.broker_fills_total ?? 0} fills, ${c.paired_round_trips ?? 0} round trips, ${c.unlinked_fills ?? 0} unlinked`);
      load();
    } catch (e) {
      toast.error(String(e?.response?.data?.detail || e.message));
    } finally { setBusy(false); }
  };

  const c = data?.counters || {};
  const health = c.health || {};
  const healthStyle = { green: "border-rd-success text-rd-success", amber: "border-amber-500 text-amber-500", red: "border-red-500 text-red-500" }[health.status] || "border-rd-border text-rd-dim";

  return (
    <div className="border border-rd-border p-3 mb-4 bg-rd-panel" data-testid="reconciliation-panel">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
        <div className="flex items-center gap-2">
          <ArrowsClockwise size={15} weight="bold" className="text-sky-400" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">Broker Reconciliation</span>
          <span className={`px-1.5 text-[9px] font-mono font-bold uppercase border ${healthStyle}`} data-testid="reconciliation-health">
            {health.status || "…"}
          </span>
          {data?.last_run && <span className="text-[9px] font-mono text-rd-dim">last run {data.last_run.slice(0, 16)}Z</span>}
        </div>
        <div className="flex gap-2">
          {c.exit_linkage_miss_count > 0 && (
            <button onClick={loadOrphans}
              className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-text transition-colors"
              data-testid="resolve-orphans-btn">
              resolve orphans ({c.exit_linkage_miss_count})
            </button>
          )}
          <button onClick={() => run(false)} disabled={busy}
            className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-wider border border-sky-500/60 text-sky-400 hover:bg-sky-500/10 transition-colors disabled:opacity-40"
            data-testid="reconciliation-run-btn">
            {busy ? "reconciling…" : "run now"}
          </button>
          <button onClick={() => run(true)} disabled={busy}
            className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-wider border border-amber-500/60 text-amber-500 hover:bg-amber-500/10 transition-colors disabled:opacity-40"
            data-testid="reconciliation-backfill-btn">
            full backfill
          </button>
        </div>
      </div>
      <div className="text-[10px] font-mono text-rd-dim mb-2">
        Broker fills are the source of truth. A real trade either becomes a completed outcome or shows here as an explicit unresolved exception — never a silent disappearance.
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1 mb-2" data-testid="reconciliation-counters">
        {COUNTER_LABELS.map(([key, label]) => (
          <span key={key} className="text-[10px] font-mono">
            <span className="text-rd-dim">{label} </span>
            <span className={key === "unlinked_fills" && c[key] > 0 ? "text-amber-500 font-bold" : "text-rd-text"}>{c[key] ?? "—"}</span>
          </span>
        ))}
        {c.reconciliation_oldest_unresolved_age_h != null && (
          <span className="text-[10px] font-mono"><span className="text-rd-dim">oldest unresolved </span><span className={c.reconciliation_oldest_unresolved_age_h > 24 ? "text-red-500" : "text-amber-500"}>{c.reconciliation_oldest_unresolved_age_h}h</span></span>
        )}
      </div>
      {(health.reasons || []).map((r, i) => (
        <div key={i} className="text-[10px] font-mono text-amber-500">⚠ {r}</div>
      ))}
      {(data?.unresolved || []).length > 0 && (
        <div className="mt-1.5" data-testid="reconciliation-unresolved">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-0.5">unresolved exceptions (retrying)</div>
          {data.unresolved.slice(0, 6).map((u) => (
            <div key={u._id} className="text-[10px] font-mono text-rd-muted">
              {u.broker} {u.side} {u.qty} {u.symbol} @ {u.price} · {String(u.ts || "").slice(0, 16)} · <span className="text-amber-500">{u.link?.status}: {u.link?.reason}</span> (×{u.link?.attempts})
            </div>
          ))}
        </div>
      )}
      {orphans && (
        <div className="mt-1.5 border border-rd-border px-2 py-1.5" data-testid="orphan-resolver">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-1">
            orphan exit resolver — supply the entry cost basis so realized P&L counts (not measured-cost eligible)
          </div>
          {(orphans.orphans || []).length === 0 && (
            <div className="text-[10px] font-mono text-rd-dim">no unresolved orphan exits</div>
          )}
          {(orphans.orphans || []).map((f) => (
            <div key={f._id} className="mb-1.5">
              <div className="text-[10px] font-mono text-rd-muted">
                {f.broker} SELL {f.qty} <span className="text-rd-text">{f.symbol}</span> @ {f.price} · {String(f.ts || "").slice(0, 16)}
                {(f.candidates || []).length > 0 && (
                  <span className="text-rd-dim"> · candidates: {f.candidates.map((p) => `${p.position_id || p.intent_id || "?"} (${String(p.opened_at || "").slice(0, 10)}${p.opened_notional_usd ? `, $${p.opened_notional_usd}` : ""})`).join(" · ")}</span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2 mt-0.5">
                <input type="number" step="any" placeholder="entry price"
                  value={resolveForm[f._id]?.price || ""}
                  onChange={(e) => setResolveForm((p) => ({ ...p, [f._id]: { ...p[f._id], price: e.target.value } }))}
                  className="w-28 bg-rd-bg border border-rd-border px-2 py-0.5 text-[11px] font-mono text-rd-text focus:border-sky-500 outline-none"
                  data-testid={`orphan-price-${f._id}`} />
                <input type="text" placeholder="note (optional)"
                  value={resolveForm[f._id]?.note || ""}
                  onChange={(e) => setResolveForm((p) => ({ ...p, [f._id]: { ...p[f._id], note: e.target.value } }))}
                  className="flex-1 min-w-[160px] bg-rd-bg border border-rd-border px-2 py-0.5 text-[11px] font-mono text-rd-text focus:border-sky-500 outline-none" />
                <button onClick={() => resolveOrphan(f._id)}
                  disabled={!parseFloat(resolveForm[f._id]?.price)}
                  className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-sky-500/60 text-sky-400 hover:bg-sky-500/10 transition-colors disabled:opacity-40"
                  data-testid={`orphan-resolve-${f._id}`}>
                  resolve
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {(scorecard?.scorecard || []).length > 0 && (
        <div className="mt-1.5" data-testid="brain-repeat-scorecard">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-0.5">brain repeat scorecard (7d) — noisiest signal emitters</div>
          {scorecard.scorecard.slice(0, 6).map((r) => (
            <div key={r.stack} className="text-[10px] font-mono">
              <span className="text-rd-text">{r.stack}</span>
              <span className="text-rd-dim"> · {r.intents} intents → {r.independent_clusters} independent · </span>
              <span className={r.repeat_ratio > 0.5 ? "text-red-500 font-bold" : r.repeat_ratio > 0.2 ? "text-amber-500" : "text-rd-success"}>
                {r.repeats} repeats ({Math.round(r.repeat_ratio * 100)}%)
              </span>
              {r.worst_cluster && (
                <span className="text-rd-dim"> · worst: {r.worst_cluster.symbol} ×{r.worst_cluster.repeats + 1} in {String(r.worst_cluster.hour || "").slice(11, 13)}:00h</span>
              )}
            </div>
          ))}
        </div>
      )}
      {(data?.recent_outcomes || []).length > 0 && (
        <div className="mt-1.5" data-testid="reconciliation-outcomes">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-0.5">recent completed outcomes (broker truth)</div>
          {data.recent_outcomes.slice(0, 6).map((o) => (
            <div key={o._id} className="text-[10px] font-mono">
              <span className="text-rd-text">{o.symbol}</span>
              <span className="text-rd-dim"> {o.qty} · </span>
              {o.entry_avg_price} → {o.exit_price}
              <span className={o.realized_pnl_usd >= 0 ? " text-rd-success" : " text-red-500"}> {o.realized_pnl_usd >= 0 ? "+" : ""}${o.realized_pnl_usd?.toFixed(4)} ({o.net_return_pct?.toFixed(2)}%)</span>
              <span className="text-rd-dim"> · fees ${o.fees_usd?.toFixed(4)} · {o.brain || o.stack || "unattributed"} · epoch {o.epoch_id}{o.measured_cost_eligible ? " · ✓cost" : ""}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default ReconciliationPanel;
