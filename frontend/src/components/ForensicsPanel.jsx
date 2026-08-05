import React, { useState } from "react";
import { api } from "@/lib/api";
import { MagnifyingGlass } from "@phosphor-icons/react";

const fmt = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);

/** Closed-trade forensic report — the diagnosis, not another rebuild.
 *  Runs against this environment's receipts; live numbers appear in prod. */
export const ForensicsPanel = () => {
  const [report, setReport] = useState(null);
  const [latency, setLatency] = useState(null);
  const [busy, setBusy] = useState(false);
  const [actuals, setActuals] = useState("");

  const run = async () => {
    setBusy(true);
    try {
      const [{ data }, { data: lat }] = await Promise.all([
        api.get("/admin/forensics/closed-trades"),
        api.get("/admin/forensics/entry-latency?n=50"),
      ]);
      setReport(data);
      setLatency(lat);
    } catch (e) { /* surfaced below */ } finally { setBusy(false); }
  };

  const saveActuals = async () => {
    const months = {};
    actuals.split(",").forEach((pair) => {
      const [m, v] = pair.split(":").map((s) => s.trim());
      if (m && v && !Number.isNaN(Number(v))) months[m] = Number(v);
    });
    if (Object.keys(months).length === 0) return;
    await api.post("/admin/forensics/broker-actuals", { months });
    run();
  };

  return (
    <div className="border border-rd-border bg-rd-panel p-3 mb-4" data-testid="forensics-panel">
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-2">
          <MagnifyingGlass size={14} weight="bold" className="text-amber-500" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Closed-Trade Forensics
          </span>
          <span className="text-[9px] font-mono uppercase text-rd-dim">since june · this environment's receipts</span>
        </div>
        <button
          onClick={run}
          disabled={busy}
          className="px-3 py-1 text-[10px] font-mono uppercase tracking-wider border border-amber-500 text-amber-500 hover:bg-amber-500/10 transition-colors disabled:opacity-40"
          data-testid="forensics-run-btn"
        >
          {busy ? "analyzing…" : "run report"}
        </button>
      </div>
      {!report ? (
        <div className="text-[10px] font-mono text-rd-dim">
          classifies every closed trade into bad selection · late entry · execution cost · exit policy, and reconciles internal P&amp;L vs broker figures per month
        </div>
      ) : (
        <>
          {report.note && (
            <div className="text-[10px] font-mono text-amber-500 mb-2" data-testid="forensics-note">{report.note}</div>
          )}
          <div className="flex flex-wrap gap-3 mb-2" data-testid="forensics-summary">
            <div className="border border-rd-border px-3 py-1.5">
              <div className="text-[9px] font-mono uppercase text-rd-dim">closed trades</div>
              <div className="font-display text-lg font-bold text-rd-text">{report.n_trades}</div>
            </div>
            <div className="border border-rd-border px-3 py-1.5">
              <div className="text-[9px] font-mono uppercase text-rd-dim">internal P&amp;L</div>
              <div className={`font-display text-lg font-bold ${report.total_pnl_usd >= 0 ? "text-rd-success" : "text-red-500"}`}>{fmt(report.total_pnl_usd)}</div>
            </div>
            {report.dominant_loss_mechanism && (
              <div className="border border-red-500 px-3 py-1.5">
                <div className="text-[9px] font-mono uppercase text-red-500">dominant loss mechanism</div>
                <div className="font-display text-lg font-bold text-red-500" data-testid="forensics-dominant">
                  {report.dominant_loss_mechanism.replaceAll("_", " ")}
                </div>
              </div>
            )}
          </div>
          {Object.keys(report.buckets || {}).length > 0 && (
            <div className="space-y-0.5 mb-2" data-testid="forensics-buckets">
              {Object.entries(report.buckets).map(([b, v]) => (
                <div key={b} className="flex gap-3 text-[10px] font-mono">
                  <span className="w-36 shrink-0 text-rd-text">{b.replaceAll("_", " ")}</span>
                  <span className="w-12 text-rd-muted">×{v.n}</span>
                  <span className={v.pnl_usd >= 0 ? "text-rd-success" : "text-red-500"}>{fmt(v.pnl_usd)}</span>
                </div>
              ))}
            </div>
          )}
          {(report.reconciliation || []).length > 0 && (
            <div className="border-t border-rd-border pt-1.5 mb-2 space-y-0.5" data-testid="forensics-recon">
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-0.5">internal vs broker (monthly)</div>
              {report.reconciliation.map((r) => (
                <div key={r.month} className="flex gap-3 text-[10px] font-mono">
                  <span className="w-16 shrink-0 text-rd-text">{r.month}</span>
                  <span className="w-24 text-rd-muted">int {fmt(r.internal_pnl_usd)}</span>
                  <span className="w-24 text-rd-muted">brk {fmt(r.broker_pnl_usd)}</span>
                  <span className={r.delta_usd == null ? "text-rd-dim" : Math.abs(r.delta_usd) < 1 ? "text-rd-success" : "text-red-500"}>
                    Δ {r.delta_usd == null ? "enter broker figure →" : fmt(r.delta_usd)}
                  </span>
                </div>
              ))}
            </div>
          )}
          {latency && (
            <div className="border-t border-rd-border pt-1.5 mb-2" data-testid="forensics-latency">
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-0.5">
                entry latency · last {latency.n} live entries
              </div>
              {latency.note ? (
                <div className="text-[10px] font-mono text-amber-500">{latency.note}</div>
              ) : (
                <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] font-mono text-rd-muted">
                  <span>signal→intent {latency.aggregates?.median_signal_to_intent_s ?? "—"}s</span>
                  <span>intent→submit {latency.aggregates?.median_intent_to_submit_s ?? "—"}s</span>
                  <span>chase {latency.aggregates?.median_chase_pct ?? "—"}%</span>
                  <span className="text-red-500">
                    bought within 1% of 30m top: {latency.aggregates?.pct_bought_within_1pct_of_30m_top ?? "—"}%
                  </span>
                </div>
              )}
              <div className="text-[9px] font-mono text-rd-dim mt-0.5">
                cadences: pulse 15s · scanner 60s · universe refresh 15m
              </div>
            </div>
          )}
          <div className="flex items-center gap-2">
            <input
              value={actuals}
              onChange={(e) => setActuals(e.target.value)}
              placeholder="broker monthly P&L e.g. 2026-06:-28.01, 2026-07:-157.29, 2026-08:-34.09"
              className="flex-1 bg-rd-bg border border-rd-border px-2 py-1 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text"
              data-testid="forensics-actuals-input"
            />
            <button
              onClick={saveActuals}
              className="px-2 py-1 text-[10px] font-mono uppercase border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-text transition-colors"
              data-testid="forensics-actuals-save"
            >
              reconcile
            </button>
          </div>
        </>
      )}
    </div>
  );
};

export default ForensicsPanel;
