import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning, ShieldCheck } from "@phosphor-icons/react";

const LANES = ["equity", "crypto"];

function LaneKnobs({ lane, policy, busy, onSave, onToggle }) {
  const p = policy[lane] || {};
  const [sl, setSl] = useState(p.sl_pct);
  const [tp, setTp] = useState(p.tp_pct);
  const [hold, setHold] = useState(p.max_hold_h);
  useEffect(() => { setSl(p.sl_pct); setTp(p.tp_pct); setHold(p.max_hold_h); },
    [p.sl_pct, p.tp_pct, p.max_hold_h]);
  return (
    <div className="flex items-center gap-2 flex-wrap py-1" data-testid={`exit-lane-${lane}`}>
      <button
        onClick={() => onToggle(lane, !p.enabled)}
        disabled={busy}
        data-testid={`exit-enable-${lane}`}
        className={`text-[10px] font-mono uppercase tracking-widest px-2 py-1 border ${p.enabled ? "bg-emerald-600 border-emerald-600 text-black" : "border-rd-border text-rd-dim hover:text-rd-text"}`}
      >
        {lane} {p.enabled ? "ARMED" : "OFF"}
      </button>
      {[["SL -%", sl, setSl, `exit-sl-${lane}`], ["TP +%", tp, setTp, `exit-tp-${lane}`], ["hold h", hold, setHold, `exit-hold-${lane}`]].map(([label, val, set, tid]) => (
        <label key={tid} className="text-[10px] font-mono text-rd-dim flex items-center gap-1">
          {label}
          <input
            value={val ?? ""}
            onChange={(e) => set(e.target.value)}
            className="w-12 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text"
            data-testid={tid}
          />
        </label>
      ))}
      <button
        onClick={() => onSave(lane, { sl_pct: Number(sl), tp_pct: Number(tp), max_hold_h: Number(hold) })}
        disabled={busy}
        data-testid={`exit-save-${lane}`}
        className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40"
      >
        save
      </button>
    </div>
  );
}

export default function ExitMonitorPanel() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get("/admin/exits");
      setData(d);
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const run = async (fn, okText) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      setMsg({ ok: true, text: okText });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  const savePolicy = (lane, fields) =>
    run(() => api.post("/admin/exits/policy", { lane, ...fields }), `${lane} exit knobs saved`);
  const toggle = (lane, enabled) =>
    run(() => api.post("/admin/exits/policy", { lane, enabled }), `${lane} exits ${enabled ? "ARMED" : "disabled"}`);
  const closeNow = (planId, sym) =>
    run(() => api.post("/admin/exits/close-now", { plan_id: planId }), `${sym} close order submitted`);

  if (!data) return null;

  const { policy, monitor, plans, scorecard } = data;
  const pct = (a, b) => (b ? (((a - b) / b) * 100).toFixed(1) : "?");

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="exit-monitor-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono flex items-center gap-1">
          <ShieldCheck size={11} />
          Exit Monitor · SL / TP / Max-Hold — {plans.length} plan{plans.length === 1 ? "" : "s"}
        </div>
        <div className="text-[9px] font-mono text-rd-dim">
          {monitor.running ? `alive · tick ${monitor.tick_count} · ${monitor.exits_submitted} exits` : "LOOP NOT RUNNING"}
        </div>
      </div>

      {LANES.map((lane) => (
        <LaneKnobs key={lane} lane={lane} policy={policy} busy={busy} onSave={savePolicy} onToggle={toggle} />
      ))}

      {msg && (
        <div
          className="mt-2 px-2 py-1 text-[10px] font-mono border"
          style={{ color: msg.ok ? "#10B981" : "#EF4444", borderColor: msg.ok ? "#10B981" : "#EF4444" }}
          data-testid="exit-msg"
        >
          {!msg.ok && <Warning size={10} className="inline mr-1" />}{msg.text}
        </div>
      )}

      {plans.length > 0 && (
        <div className="mt-2 border-t border-rd-border/50 pt-2" data-testid="exit-plans">
          <div className="grid grid-cols-[80px_36px_1fr_1fr_1fr_1fr_70px_60px] gap-1 text-[9px] uppercase tracking-widest text-rd-dim font-mono pb-1">
            <span>symbol</span><span>lane</span><span>entry</span><span>stop</span><span>target</span><span>hold until</span><span>status</span><span></span>
          </div>
          {plans.map((p) => (
            <div
              key={p.plan_id}
              className="grid grid-cols-[80px_36px_1fr_1fr_1fr_1fr_70px_60px] gap-1 text-[10px] font-mono py-0.5 items-center"
              data-testid={`exit-plan-${p.symbol?.split("/")[0]}`}
            >
              <span className="font-bold text-rd-text truncate">{p.symbol}</span>
              <span className="text-rd-dim">{p.lane === "crypto" ? "cr" : "eq"}</span>
              <span className="text-rd-dim">${Number(p.entry_price).toFixed(p.entry_price < 1 ? 5 : 2)}</span>
              <span className="text-rd-danger">${Number(p.stop_price).toFixed(p.stop_price < 1 ? 5 : 2)} ({pct(p.stop_price, p.entry_price)}%)</span>
              <span className="text-emerald-500">${Number(p.target_price).toFixed(p.target_price < 1 ? 5 : 2)} (+{pct(p.target_price, p.entry_price)}%)</span>
              <span className="text-rd-dim">{(p.max_hold_until || "").slice(5, 16).replace("T", " ")}</span>
              <span className={p.status === "error" ? "text-rd-danger" : "text-rd-dim"}>
                {p.status}{p.levels_source === "brain" ? " ·🧠" : ""}
              </span>
              <button
                onClick={() => closeNow(p.plan_id, p.symbol)}
                disabled={busy || p.status === "error"}
                data-testid={`exit-close-now-${p.symbol?.split("/")[0]}`}
                className="text-[9px] font-mono uppercase border border-rd-border hover:border-rd-danger hover:text-rd-danger px-1 py-0.5 disabled:opacity-40"
              >
                close
              </button>
            </div>
          ))}
        </div>
      )}

      {(scorecard || []).length > 0 && (
        <div className="mt-2 border-t border-rd-border/50 pt-2" data-testid="exit-scorecard">
          <div className="text-[9px] uppercase tracking-widest text-rd-dim font-mono pb-1">
            Brain scorecard · realized outcomes (30d) — folds into arbiter seat weights
          </div>
          <div className="grid grid-cols-[90px_36px_50px_90px_60px_70px_80px] gap-1 text-[9px] uppercase tracking-widest text-rd-dim font-mono pb-0.5">
            <span>brain</span><span>lane</span><span>closed</span><span>tp/sl/to</span><span>avg %</span><span>pnl $</span><span></span>
          </div>
          {scorecard.map((s) => (
            <div
              key={`${s.brain}-${s.lane}`}
              className="grid grid-cols-[90px_36px_50px_90px_60px_70px_80px] gap-1 text-[10px] font-mono py-0.5"
              data-testid={`exit-scorecard-${s.brain}-${s.lane}`}
            >
              <span className="font-bold text-rd-text">{s.brain}</span>
              <span className="text-rd-dim">{s.lane === "crypto" ? "cr" : "eq"}</span>
              <span className="text-rd-dim">{s.closed}</span>
              <span className="text-rd-dim">
                <span className="text-emerald-500">{s.tp_hit}</span>/
                <span className="text-rd-danger">{s.sl_hit}</span>/{s.timeout}
              </span>
              <span className={s.avg_pnl_pct >= 0 ? "text-emerald-500" : "text-rd-danger"}>
                {s.avg_pnl_pct != null ? `${s.avg_pnl_pct > 0 ? "+" : ""}${s.avg_pnl_pct.toFixed(2)}` : "—"}
              </span>
              <span className={s.total_pnl_usd >= 0 ? "text-emerald-500" : "text-rd-danger"}>
                {s.total_pnl_usd != null ? `${s.total_pnl_usd > 0 ? "+" : ""}${s.total_pnl_usd.toFixed(2)}` : "—"}
              </span>
              <span></span>
            </div>
          ))}
        </div>
      )}

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Broker positions are reconciled every {Math.round(monitor.interval_sec || 20)}s; every confirmed position gets an exit plan
        (brain-authored levels when present, else lane defaults — never unbounded). SL exits at MARKET; TP/max-hold use marketable
        LIMIT escalating to MARKET (crypto). Equity exits are MARKET (Webull fractional rule) and fire during RTH only. Receipts are permanent.
      </div>
    </div>
  );
}
