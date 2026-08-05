import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { TrendDown, Warning } from "@phosphor-icons/react";

const PATTERNS = ["double_top", "head_shoulders", "rising_wedge"];
const relTime = (ts) => {
  if (!ts) return "—";
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

/** Sell-Point Watcher — bearish structures on HELD tickers.
 *  OBSERVE mode logs receipts only; ACT applies tighten/exit. */
export const SellPointPanel = () => {
  const [cfg, setCfg] = useState(null);
  const [events, setEvents] = useState([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = () =>
    api.get("/admin/sell-point").then(({ data }) => {
      setCfg(data.config);
      setEvents(data.events || []);
    }).catch(() => {});

  useEffect(() => { load(); }, []);

  const save = async (changes) => {
    setBusy(true); setMsg(null);
    try {
      const { data } = await api.post("/admin/sell-point", changes);
      setCfg(data.config);
      setMsg({ ok: true, text: "sell-point knobs saved" });
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally { setBusy(false); }
  };

  const toggleMode = () => {
    if (!cfg) return;
    const next = cfg.mode === "act" ? "observe" : "act";
    if (next === "act" && !window.confirm(
      "Switch Sell-Point Watcher to ACT mode? Detected patterns will TIGHTEN stops (or EXIT, per pattern) on live positions.",
    )) return;
    save({ mode: next });
  };

  return (
    <div className="border border-rd-border bg-rd-panel p-3 mb-4" data-testid="sell-point-panel">
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-2">
          <TrendDown size={14} weight="bold" className="text-red-400" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Sell-Point Watcher
          </span>
          <span
            className={`px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border ${
              cfg?.mode === "act" ? "border-red-500 text-red-500" : "border-sky-400 text-sky-400"
            }`}
            data-testid="sell-point-mode-badge"
          >
            {cfg ? cfg.mode : "…"}
          </span>
        </div>
        <button
          onClick={toggleMode}
          disabled={busy || !cfg}
          className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-text transition-colors"
          data-testid="sell-point-mode-toggle"
        >
          {cfg?.mode === "act" ? "switch to observe" : "arm ACT mode"}
        </button>
      </div>
      <div className="text-[10px] font-mono text-rd-dim mb-2">
        Double top · head &amp; shoulders · rising wedge on held tickers. Observe logs receipts; ACT raises stops (never lowers) or exits per pattern.
      </div>
      {cfg && (
        <div className="flex flex-wrap items-end gap-3 mb-2">
          {PATTERNS.map((p) => (
            <label key={p} className="flex flex-col gap-0.5">
              <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">{p.replace("_", " ")}</span>
              <select
                value={cfg.actions?.[p] || "tighten"}
                onChange={(e) => save({ actions: { [p]: e.target.value } })}
                className="bg-rd-bg border border-rd-border px-2 py-1 text-xs font-mono text-rd-text focus:outline-none focus:border-rd-text"
                data-testid={`sell-point-action-${p}`}
              >
                <option value="off">off</option>
                <option value="tighten">tighten</option>
                <option value="exit">exit</option>
              </select>
            </label>
          ))}
          <label className="flex flex-col gap-0.5">
            <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">stop buffer ×ATR</span>
            <input
              type="number"
              step="0.1"
              value={cfg.stop_buffer_atr ?? ""}
              onChange={(e) => setCfg({ ...cfg, stop_buffer_atr: e.target.value })}
              onBlur={() => save({ stop_buffer_atr: Number(cfg.stop_buffer_atr) })}
              className="w-20 bg-rd-bg border border-rd-border px-2 py-1 text-xs font-mono text-rd-text focus:outline-none focus:border-rd-text"
              data-testid="sell-point-buffer"
            />
          </label>
        </div>
      )}
      {events.length > 0 ? (
        <div className="border-t border-rd-border pt-1.5 space-y-0.5" data-testid="sell-point-events">
          {events.slice(0, 6).map((e) => (
            <div key={e._id} className="flex items-center gap-2 text-[10px] font-mono" data-testid={`sell-point-event-${e._id}`}>
              <span className="w-20 shrink-0 text-rd-text">{e.symbol}</span>
              <span className="px-1 border border-red-500 text-red-500 text-[8px] uppercase">{e.pattern}</span>
              <span className={e.applied ? "text-rd-success" : "text-rd-dim"}>
                {e.applied ? `${e.action} applied${e.new_stop ? ` → stop ${e.new_stop}` : ""}` : `${e.action} · ${e.detail || "logged"}`}
              </span>
              <span className="ml-auto text-rd-dim">{relTime(e.created_at)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-[10px] font-mono text-rd-dim" data-testid="sell-point-empty">
          no bearish structures detected on held tickers yet
        </div>
      )}
      {msg && (
        <div
          className={`mt-1.5 text-[10px] font-mono flex items-center gap-1 ${msg.ok ? "text-rd-success" : "text-red-500"}`}
          data-testid="sell-point-msg"
        >
          {!msg.ok && <Warning size={10} weight="bold" />} {msg.text}
        </div>
      )}
    </div>
  );
};

export default SellPointPanel;
