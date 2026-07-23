import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Scales, Warning } from "@phosphor-icons/react";

const WINDOWS = [7, 30, 90];

const fmt = (v, plus = true) =>
  v == null ? "—" : `${plus && v > 0 ? "+" : ""}${Number(v).toFixed(2)}`;
const cls = (v) =>
  v == null ? "text-rd-dim" : v >= 0 ? "text-emerald-500" : "text-rd-danger";

function Tile({ label, value, color, tid }) {
  return (
    <div className="border border-rd-border/60 px-2 py-1" data-testid={tid}>
      <div className="text-[8px] uppercase tracking-widest text-rd-dim font-mono">{label}</div>
      <div className={`text-[13px] font-mono font-bold ${color || "text-rd-text"}`}>{value}</div>
    </div>
  );
}

function LaneConfig({ lane, cfg, busy, onSave }) {
  const c = cfg[lane] || {};
  const [fee, setFee] = useState(c.taker_fee_pct);
  const [spr, setSpr] = useState(c.spread_bps);
  useEffect(() => { setFee(c.taker_fee_pct); setSpr(c.spread_bps); },
    [c.taker_fee_pct, c.spread_bps]);
  return (
    <div className="flex items-center gap-2 text-[10px] font-mono text-rd-dim" data-testid={`expectancy-cfg-${lane}`}>
      <span className="uppercase tracking-widest w-12">{lane}</span>
      <label className="flex items-center gap-1">fee %/side
        <input value={fee ?? ""} onChange={(e) => setFee(e.target.value)}
          className="w-12 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text"
          data-testid={`expectancy-fee-${lane}`} />
      </label>
      <label className="flex items-center gap-1">spread bps
        <input value={spr ?? ""} onChange={(e) => setSpr(e.target.value)}
          className="w-12 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text"
          data-testid={`expectancy-spread-${lane}`} />
      </label>
      <button onClick={() => onSave(lane, { taker_fee_pct: Number(fee), spread_bps: Number(spr) })}
        disabled={busy} data-testid={`expectancy-save-${lane}`}
        className="text-[9px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40">
        save
      </button>
    </div>
  );
}

export default function ExpectancyPanel() {
  const [data, setData] = useState(null);
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async (d = days) => {
    try {
      const { data: r } = await api.get(`/admin/expectancy?days=${d}`);
      setData(r);
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, [days]);

  useEffect(() => {
    load();
    const t = setInterval(() => load(), 60000);
    return () => clearInterval(t);
  }, [load]);

  const saveCfg = async (lane, fields) => {
    setBusy(true); setMsg(null);
    try {
      await api.post("/admin/expectancy/config", { lane, ...fields });
      setMsg({ ok: true, text: `${lane} cost model saved` });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!data) return null;
  const o = data.overall || {};
  const lanes = data.by_lane || {};
  const brains = data.by_brain || [];
  const conf = data.by_confluence || {};
  const confModes = Object.keys(conf).filter((m) => conf[m].trades > 0);

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="expectancy-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono flex items-center gap-1">
          <Scales size={11} />
          Expectancy · realized P&L after fee + spread drag
        </div>
        <div className="flex items-center gap-1">
          {WINDOWS.map((w) => (
            <button key={w} onClick={() => { setDays(w); load(w); }}
              data-testid={`expectancy-window-${w}`}
              className={`text-[9px] font-mono px-1.5 py-0.5 border ${days === w ? "border-rd-text text-rd-text" : "border-rd-border text-rd-dim hover:text-rd-text"}`}>
              {w}d
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-4 sm:grid-cols-8 gap-1 mb-2">
        <Tile label="net $" value={fmt(o.net_pnl_usd)} color={cls(o.net_pnl_usd)} tid="expectancy-net" />
        <Tile label="gross $" value={fmt(o.gross_pnl_usd)} color={cls(o.gross_pnl_usd)} tid="expectancy-gross" />
        <Tile label="fees $" value={fmt(o.est_fees_usd == null ? null : -o.est_fees_usd, false)} color="text-amber-500" tid="expectancy-fees" />
        <Tile label="spread $" value={fmt(o.est_spread_usd == null ? null : -o.est_spread_usd, false)} color="text-amber-500" tid="expectancy-spread" />
        <Tile label="exp/trade" value={fmt(o.expectancy_usd)} color={cls(o.expectancy_usd)} tid="expectancy-per-trade" />
        <Tile label="trades" value={o.trades ?? 0} tid="expectancy-trades" />
        <Tile label="win %" value={o.win_rate_pct == null ? "—" : o.win_rate_pct.toFixed(1)} tid="expectancy-winrate" />
        <Tile label="pf" value={o.profit_factor == null ? "—" : o.profit_factor.toFixed(2)} tid="expectancy-pf" />
      </div>

      {Object.keys(lanes).length > 0 && (
        <div className="border-t border-rd-border/50 pt-1 mb-2" data-testid="expectancy-lanes">
          {Object.entries(lanes).map(([lane, l]) => (
            <div key={lane} className="grid grid-cols-[48px_60px_1fr_1fr_1fr_1fr_60px] gap-1 text-[10px] font-mono py-0.5" data-testid={`expectancy-lane-${lane}`}>
              <span className="uppercase text-rd-dim">{lane === "crypto" ? "crypto" : "equity"}</span>
              <span className="text-rd-dim">{l.trades} tr</span>
              <span className={cls(l.gross_pnl_usd)}>g {fmt(l.gross_pnl_usd)}</span>
              <span className="text-amber-500">-f {l.est_fees_usd?.toFixed(2)}</span>
              <span className="text-amber-500">-s {l.est_spread_usd?.toFixed(2)}</span>
              <span className={`font-bold ${cls(l.net_pnl_usd)}`}>n {fmt(l.net_pnl_usd)}</span>
              <span className="text-rd-dim">{l.win_rate_pct == null ? "—" : `${l.win_rate_pct}%`}</span>
            </div>
          ))}
        </div>
      )}

      {brains.length > 0 && (
        <div className="border-t border-rd-border/50 pt-1 mb-2" data-testid="expectancy-brains">
          <div className="grid grid-cols-[90px_36px_50px_70px_70px_70px_60px] gap-1 text-[9px] uppercase tracking-widest text-rd-dim font-mono pb-0.5">
            <span>brain</span><span>lane</span><span>trades</span><span>net $</span><span>exp/tr</span><span>costs $</span><span>win %</span>
          </div>
          {brains.map((b) => (
            <div key={`${b.brain}-${b.lane}`} className="grid grid-cols-[90px_36px_50px_70px_70px_70px_60px] gap-1 text-[10px] font-mono py-0.5" data-testid={`expectancy-brain-${b.brain}-${b.lane}`}>
              <span className="font-bold text-rd-text">{b.brain}</span>
              <span className="text-rd-dim">{b.lane === "crypto" ? "cr" : "eq"}</span>
              <span className="text-rd-dim">{b.trades}</span>
              <span className={`font-bold ${cls(b.net_pnl_usd)}`}>{fmt(b.net_pnl_usd)}</span>
              <span className={cls(b.expectancy_usd)}>{fmt(b.expectancy_usd)}</span>
              <span className="text-amber-500">{((b.est_fees_usd || 0) + (b.est_spread_usd || 0)).toFixed(2)}</span>
              <span className="text-rd-dim">{b.win_rate_pct == null ? "—" : `${b.win_rate_pct}%`}</span>
            </div>
          ))}
        </div>
      )}

      {confModes.length > 0 && (
        <div className="border-t border-rd-border/50 pt-1 mb-2" data-testid="expectancy-confluence">
          <div className="text-[9px] uppercase tracking-widest text-rd-dim font-mono pb-0.5">
            confluence mode · full vs 2/3 half-size probes
          </div>
          {confModes.map((m) => (
            <div key={m} className="grid grid-cols-[90px_50px_70px_70px_60px] gap-1 text-[10px] font-mono py-0.5" data-testid={`expectancy-conf-${m}`}>
              <span className="text-rd-text">{m}</span>
              <span className="text-rd-dim">{conf[m].trades}</span>
              <span className={`font-bold ${cls(conf[m].net_pnl_usd)}`}>{fmt(conf[m].net_pnl_usd)}</span>
              <span className={cls(conf[m].expectancy_usd)}>{fmt(conf[m].expectancy_usd)}</span>
              <span className="text-rd-dim">{conf[m].win_rate_pct == null ? "—" : `${conf[m].win_rate_pct}%`}</span>
            </div>
          ))}
        </div>
      )}

      <div className="border-t border-rd-border/50 pt-2 flex flex-col gap-1">
        {["crypto", "equity"].map((lane) => (
          <LaneConfig key={lane} lane={lane} cfg={data.config || {}} busy={busy} onSave={saveCfg} />
        ))}
      </div>

      {msg && (
        <div className="mt-2 px-2 py-1 text-[10px] font-mono border"
          style={{ color: msg.ok ? "#10B981" : "#EF4444", borderColor: msg.ok ? "#10B981" : "#EF4444" }}
          data-testid="expectancy-msg">
          {!msg.ok && <Warning size={10} className="inline mr-1" />}{msg.text}
        </div>
      )}

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Cost model: fees = fee%/side × (entry+exit) notional; spread = half the quoted spread paid on each side.
        A win is net-positive AFTER costs. {data.unpriced_rows > 0 ? `${data.unpriced_rows} rows lacked P&L and were excluded. ` : ""}
        Options lane stays gated until this panel shows positive expectancy.
      </div>
    </div>
  );
}
