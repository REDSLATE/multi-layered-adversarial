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
  const [broker, setBroker] = useState(null);
  const [brokerBusy, setBrokerBusy] = useState(false);
  const [brokerStart, setBrokerStart] = useState("2026-06-01");

  const runBroker = async () => {
    setBrokerBusy(true);
    try {
      const { data } = await api.get(
        `/admin/forensics/broker-report?start=${brokerStart}`
      );
      setBroker(data);
    } catch (e) {
      setBroker({ ok: false, error: e?.response?.data?.detail || String(e) });
    } finally { setBrokerBusy(false); }
  };

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
      <div className="border-t border-rd-border mt-2 pt-2" data-testid="broker-forensics-section">
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono font-bold uppercase tracking-widest text-rd-text">
              Broker Forensics · Webull
            </span>
            <span className="text-[9px] font-mono uppercase text-rd-dim">round trips rebuilt from broker fills · DB-independent</span>
          </div>
          <div className="flex items-center gap-2">
            <input
              value={brokerStart}
              onChange={(e) => setBrokerStart(e.target.value)}
              placeholder="yyyy-MM-dd"
              className="w-24 bg-rd-bg border border-rd-border px-2 py-1 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text"
              data-testid="broker-forensics-start-input"
            />
            <button
              onClick={runBroker}
              disabled={brokerBusy}
              className="px-3 py-1 text-[10px] font-mono uppercase tracking-wider border border-cyan-500 text-cyan-500 hover:bg-cyan-500/10 transition-colors disabled:opacity-40"
              data-testid="broker-forensics-run-btn"
            >
              {brokerBusy ? "pulling fills…" : "pull broker history"}
            </button>
          </div>
        </div>
        {!broker ? (
          <div className="text-[10px] font-mono text-rd-dim">
            pulls Webull's own filled-order history, FIFO-matches buys→sells, and shows realized P&amp;L, fees, hold times and unmanaged-hold losses — works even when internal receipts are empty
          </div>
        ) : !broker.ok ? (
          <div className="text-[10px] font-mono text-red-500" data-testid="broker-forensics-error">{broker.error}</div>
        ) : (
          <>
            {broker.note && (
              <div className="text-[10px] font-mono text-amber-500 mb-1.5" data-testid="broker-forensics-note">{broker.note}</div>
            )}
            <div className="flex flex-wrap gap-3 mb-2" data-testid="broker-forensics-summary">
              <div className="border border-rd-border px-3 py-1.5">
                <div className="text-[9px] font-mono uppercase text-rd-dim">round trips</div>
                <div className="font-display text-lg font-bold text-rd-text">{broker.n_round_trips}</div>
              </div>
              <div className="border border-rd-border px-3 py-1.5">
                <div className="text-[9px] font-mono uppercase text-rd-dim">realized P&amp;L</div>
                <div className={`font-display text-lg font-bold ${broker.realized_pnl_usd >= 0 ? "text-rd-success" : "text-red-500"}`}>{fmt(broker.realized_pnl_usd)}</div>
              </div>
              <div className="border border-rd-border px-3 py-1.5">
                <div className="text-[9px] font-mono uppercase text-rd-dim">fees</div>
                <div className="font-display text-lg font-bold text-rd-muted">{fmt(broker.total_fees_usd)}</div>
              </div>
              <div className="border border-rd-border px-3 py-1.5">
                <div className="text-[9px] font-mono uppercase text-rd-dim">win rate</div>
                <div className="font-display text-lg font-bold text-rd-text">{broker.win_rate_pct == null ? "—" : `${broker.win_rate_pct}%`}</div>
              </div>
              <div className="border border-rd-border px-3 py-1.5">
                <div className="text-[9px] font-mono uppercase text-rd-dim">median hold</div>
                <div className="font-display text-lg font-bold text-rd-text">{broker.median_hold_h == null ? "—" : `${broker.median_hold_h}h`}</div>
              </div>
              {broker.dominant_loss_mechanism && (
                <div className="border border-red-500 px-3 py-1.5">
                  <div className="text-[9px] font-mono uppercase text-red-500">dominant loss mechanism</div>
                  <div className="font-display text-lg font-bold text-red-500" data-testid="broker-forensics-dominant">
                    {broker.dominant_loss_mechanism.replaceAll("_", " ")}
                  </div>
                </div>
              )}
            </div>
            {Object.keys(broker.buckets || {}).length > 0 && (
              <div className="space-y-0.5 mb-2" data-testid="broker-forensics-buckets">
                {Object.entries(broker.buckets).map(([b, v]) => (
                  <div key={b} className="flex gap-3 text-[10px] font-mono">
                    <span className="w-36 shrink-0 text-rd-text">{b.replaceAll("_", " ")}</span>
                    <span className="w-12 text-rd-muted">×{v.n}</span>
                    <span className={v.pnl_usd >= 0 ? "text-rd-success" : "text-red-500"}>{fmt(v.pnl_usd)}</span>
                  </div>
                ))}
              </div>
            )}
            {Object.keys(broker.monthly_pnl || {}).length > 0 && (
              <div className="border-t border-rd-border pt-1.5 mb-2 space-y-0.5" data-testid="broker-forensics-monthly">
                <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-0.5">broker realized P&amp;L (monthly)</div>
                {Object.entries(broker.monthly_pnl).map(([m, v]) => (
                  <div key={m} className="flex gap-3 text-[10px] font-mono">
                    <span className="w-16 shrink-0 text-rd-text">{m}</span>
                    <span className={v >= 0 ? "text-rd-success" : "text-red-500"}>{fmt(v)}</span>
                  </div>
                ))}
              </div>
            )}
            {(broker.round_trips || []).length > 0 && (
              <div className="border-t border-rd-border pt-1.5 mb-1" data-testid="broker-forensics-trades">
                <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-0.5">worst round trips</div>
                {[...broker.round_trips].sort((a, b) => a.pnl_usd - b.pnl_usd).slice(0, 10).map((t, i) => (
                  <div key={i} className="flex gap-3 text-[10px] font-mono">
                    <span className="w-14 shrink-0 text-rd-text">{t.symbol}</span>
                    <span className="w-28 text-rd-muted">{(t.exit_at || "").slice(0, 10)}</span>
                    <span className="w-20 text-rd-muted">{t.hold_h == null ? "—" : `${t.hold_h}h held`}</span>
                    <span className={`w-20 ${t.pnl_usd >= 0 ? "text-rd-success" : "text-red-500"}`}>{fmt(t.pnl_usd)}</span>
                    <span className="text-rd-dim">{(t.verdict || "").replaceAll("_", " ")}</span>
                  </div>
                ))}
              </div>
            )}
            {(broker.open_lots || []).length > 0 && (
              <div className="text-[10px] font-mono text-amber-500" data-testid="broker-forensics-open">
                {broker.open_lots.length} unclosed buy lot(s) at the broker: {broker.open_lots.map((l) => l.symbol).join(", ")}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
};

export default ForensicsPanel;
