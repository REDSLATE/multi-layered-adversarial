import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Circuitry } from "@phosphor-icons/react";

const num = (v) => (v === "" || v == null ? null : Number(v));
const inp = "w-16 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text";

function Dot({ ok, label, tid }) {
  return (
    <span className="flex items-center gap-1 text-[9px] font-mono uppercase tracking-widest text-rd-dim" data-testid={tid}>
      <span className={`inline-block w-1.5 h-1.5 rounded-full ${ok ? "bg-emerald-500" : "bg-rd-danger"}`} />
      {label}
    </span>
  );
}

function Cell({ label, value, cls }) {
  return (
    <div className="flex flex-col">
      <span className="text-[8px] font-mono uppercase tracking-widest text-rd-dim/60">{label}</span>
      <span className={`text-[10px] font-mono ${cls || "text-rd-text"}`}>{value}</span>
    </div>
  );
}

export default function OptionsPanel() {
  const [status, setStatus] = useState(null);
  const [knobs, setKnobs] = useState({});
  const [underlying, setUnderlying] = useState("AAPL");
  const [action, setAction] = useState("BUY");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: r } = await api.get("/admin/options/status");
      setStatus(r);
      const p = r.policy || {};
      setKnobs({
        risk_fraction: p.risk_fraction,
        premium_stop_fraction: p.premium_stop_fraction,
        max_premium_fraction: p.max_premium_fraction,
        target_abs_delta: p.target_abs_delta,
        min_dte: p.min_dte,
        max_dte: p.max_dte,
      });
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const saveKnobs = async () => {
    setBusy(true);
    try {
      const payload = Object.fromEntries(
        Object.entries(knobs).filter(([, v]) => v !== "" && v != null)
          .map(([k, v]) => [k, Number(v)]),
      );
      await api.post("/admin/risk-sizer/policy", { options: payload });
      setMsg({ ok: true, text: "options knobs saved" });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally { setBusy(false); }
  };

  const resolve = async () => {
    setBusy(true); setRes(null); setMsg(null);
    try {
      const { data: r } = await api.get("/admin/options/resolve", {
        params: { underlying: underlying.trim().toUpperCase(), action },
        timeout: 90000,
      });
      setRes(r);
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally { setBusy(false); }
  };

  if (!status) return null;
  const ent = status.entitlements || {};
  const c = res?.contract;
  const s = res?.sizing;
  const spreadPct = c && c.bid && c.ask ? (((c.ask - c.bid) / ((c.ask + c.bid) / 2)) * 100).toFixed(1) : null;

  return (
    <div className="border border-rd-border p-3 space-y-2" data-testid="options-panel">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Circuitry size={14} className="text-rd-dim" />
          <span className="text-[11px] font-mono uppercase tracking-widest text-rd-text">Options Lane</span>
          <span className="text-[9px] font-mono text-rd-dim/70">premium-based sizing · fail-closed feed</span>
        </div>
        <div className="flex items-center gap-3">
          <Dot ok={!!status.lane_enabled} label="lane" tid="options-lane-light" />
          <Dot ok={ent.us_option_quotes === true} label="OPRA feed" tid="options-opra-light" />
        </div>
      </div>

      {msg && (
        <div className={`text-[10px] font-mono ${msg.ok ? "text-emerald-500" : "text-rd-danger"}`}
          data-testid="options-msg">{msg.text}</div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-[10px] font-mono text-rd-dim" data-testid="options-knobs">
        <label className="flex items-center gap-1">risk
          <input value={knobs.risk_fraction ?? ""} className={inp} data-testid="options-knob-risk"
            onChange={(e) => setKnobs({ ...knobs, risk_fraction: e.target.value })} />
        </label>
        <label className="flex items-center gap-1">prem stop
          <input value={knobs.premium_stop_fraction ?? ""} className={inp} data-testid="options-knob-prem-stop"
            onChange={(e) => setKnobs({ ...knobs, premium_stop_fraction: e.target.value })} />
        </label>
        <label className="flex items-center gap-1">max prem
          <input value={knobs.max_premium_fraction ?? ""} className={inp} data-testid="options-knob-max-prem"
            onChange={(e) => setKnobs({ ...knobs, max_premium_fraction: e.target.value })} />
        </label>
        <label className="flex items-center gap-1">Δ target
          <input value={knobs.target_abs_delta ?? ""} className={inp} data-testid="options-knob-delta"
            onChange={(e) => setKnobs({ ...knobs, target_abs_delta: e.target.value })} />
        </label>
        <label className="flex items-center gap-1">dte
          <input value={knobs.min_dte ?? ""} className="w-10 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none"
            data-testid="options-knob-min-dte"
            onChange={(e) => setKnobs({ ...knobs, min_dte: e.target.value })} />
          –
          <input value={knobs.max_dte ?? ""} className="w-10 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none"
            data-testid="options-knob-max-dte"
            onChange={(e) => setKnobs({ ...knobs, max_dte: e.target.value })} />
        </label>
        <button disabled={busy} onClick={saveKnobs} data-testid="options-knobs-save"
          className="text-[9px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40">
          save
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-rd-border/40">
        <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim/70">contract dry-run</span>
        <input value={underlying} onChange={(e) => setUnderlying(e.target.value)}
          className="w-20 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text uppercase focus:outline-none focus:border-rd-text"
          data-testid="options-dryrun-underlying" placeholder="AAPL" />
        <select value={action} onChange={(e) => setAction(e.target.value)}
          className="bg-rd-bg border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text"
          data-testid="options-dryrun-action">
          <option value="BUY">CALL (long)</option>
          <option value="SHORT">PUT (short)</option>
        </select>
        <button disabled={busy || !underlying.trim()} onClick={resolve} data-testid="options-dryrun-resolve"
          className="text-[9px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40">
          {busy ? "resolving…" : "resolve"}
        </button>
      </div>

      {res && !c && (
        <div className="text-[10px] font-mono text-rd-danger" data-testid="options-dryrun-fail">
          no contract · {res.reason}
          {res.spot ? ` · spot $${res.spot}` : ""}
          {res.considered ? ` · ${res.considered} considered` : ""}
        </div>
      )}

      {c && (
        <div className="border border-rd-border/60 p-2 space-y-2" data-testid="options-dryrun-contract">
          <div className="text-[10px] font-mono text-rd-text">{c.symbol}
            <span className="text-rd-dim"> · spot ${res.spot}</span>
          </div>
          <div className="grid grid-cols-4 sm:grid-cols-8 gap-2">
            <Cell label="premium" value={`$${c.premium?.toFixed(2)}`} />
            <Cell label="bid/ask" value={`${c.bid} / ${c.ask}`} />
            <Cell label="spread" value={spreadPct ? `${spreadPct}%` : "—"} />
            <Cell label="OI" value={c.open_interest} />
            <Cell label="Δ" value={c.delta?.toFixed(3)} />
            <Cell label="Θ" value={c.theta?.toFixed(3)} />
            <Cell label="IV" value={c.imp_vol ? `${(c.imp_vol * 100).toFixed(1)}%` : "—"} />
            <Cell label="DTE" value={c.dte} />
          </div>
          {s && (
            <div className={`text-[10px] font-mono ${s.approved ? "text-emerald-500" : "text-amber-400"}`}
              data-testid="options-dryrun-sizing">
              {s.approved
                ? `WOULD SIZE: ${s.contracts} contract${s.contracts > 1 ? "s" : ""} · $${s.final_notional} · risk $${s.risk_budget} of $${s.risk_budget_max} budget (${s.balance_source} eq $${s.account_equity})`
                : `WOULD NOT SIZE: ${s.reason}${s.risk_budget_max != null ? ` · budget $${s.risk_budget_max}` : ""}${s.per_contract_cost != null ? ` vs $${s.per_contract_cost}/contract` : ""}`}
              {s.roadguard_clear === false && (
                <span className="text-rd-danger"> · ROADGUARD FROZEN (live entries blocked)</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
