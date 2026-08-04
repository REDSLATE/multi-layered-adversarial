import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ShieldCheck, Plus, X, Warning } from "@phosphor-icons/react";

const relTime = (ts) => {
  if (!ts) return "—";
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

/** BUY Allowlist — the crypto lane may only BUY these pairs.
 *  SELLs / exits are never gated. Held intents keep their full
 *  doctrine evidence for review. */
export const BuyAllowlistPanel = () => {
  const [al, setAl] = useState(null);
  const [audit, setAudit] = useState([]);
  const [held, setHeld] = useState(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [elig, setElig] = useState(null);
  const [eligBusy, setEligBusy] = useState(false);
  const [eligMsg, setEligMsg] = useState(null);

  const load = useCallback(async () => {
    try {
      const [{ data: a }, { data: h }, { data: e }] = await Promise.all([
        api.get("/admin/universe/crypto-buy-allowlist"),
        api.get("/admin/universe/crypto-buy-allowlist/held-stats"),
        api.get("/admin/universe/buy-eligibility"),
      ]);
      setAl(a.allowlist);
      setAudit(a.audit || []);
      setHeld(h);
      setElig(e.config);
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

  const saveElig = async () => {
    if (!elig) return;
    setEligBusy(true); setEligMsg(null);
    try {
      const { data } = await api.post("/admin/universe/buy-eligibility", {
        mode: elig.mode,
        min_dollar_vol_24h: Number(elig.min_dollar_vol_24h),
        max_spread_bps: Number(elig.max_spread_bps),
        max_notional_usd: Number(elig.max_notional_usd),
        max_pct_of_24h_vol: Number(elig.max_pct_of_24h_vol),
      });
      setElig(data.config);
      setEligMsg({ ok: true, text: `saved · max $${Number(data.config.max_notional_usd).toFixed(2)} / trade on every crypto BUY` });
    } catch (e) {
      setEligMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally { setEligBusy(false); }
  };

  useEffect(() => { load(); }, [load]);

  const save = async (enabled, symbols) => {
    setBusy(true); setMsg(null);
    try {
      const { data } = await api.put("/admin/universe/crypto-buy-allowlist", { enabled, symbols });
      setAl(data.allowlist);
      setMsg({ ok: true, text: "allowlist updated" });
      load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally { setBusy(false); }
  };

  const add = () => {
    const v = input.trim();
    if (!v || !al) return;
    setInput("");
    save(al.enabled, [...al.symbols, v]);
  };
  const remove = (sym) => al && save(al.enabled, al.symbols.filter((s) => s !== sym));
  const toggle = () => {
    if (!al) return;
    const next = !al.enabled;
    if (!next && !window.confirm(
      "Disable the BUY allowlist? The crypto lane will BUY anything the movers list surfaces.",
    )) return;
    save(next, al.symbols);
  };

  const lastChange = audit.find((a) => a.kind !== "override_policy");

  return (
    <div className="border border-rd-border bg-rd-panel p-3 mb-4" data-testid="buy-allowlist-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <ShieldCheck size={14} weight="bold" className="text-rd-success" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Crypto BUY Allowlist
          </span>
          <span
            className={`px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border ${
              al?.enabled ? "border-rd-success text-rd-success" : "border-amber-500 text-amber-500"
            }`}
            data-testid="buy-allowlist-status"
          >
            {al ? (al.enabled ? "ENFORCING" : "DISABLED") : "…"}
          </span>
        </div>
        <button
          onClick={toggle}
          disabled={busy || !al}
          className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-text transition-colors"
          data-testid="buy-allowlist-toggle-btn"
        >
          {al?.enabled ? "disable" : "enable"}
        </button>
      </div>

      <div className="text-[10px] font-mono text-rd-dim mb-2">
        Crypto lane may only BUY these pairs — the movers list is watch-only. SELLs / exits are never gated.
      </div>

      {held && (
        <div className="flex gap-4 mb-3" data-testid="buy-allowlist-held-counters">
          {["1h", "24h", "7d"].map((w) => (
            <div key={w} className="border border-rd-border px-3 py-1.5" data-testid={`buy-allowlist-held-${w}`}>
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">held {w}</div>
              <div className="font-display text-lg font-bold text-amber-500 leading-none">
                {held.held_counts?.[w] ?? "—"}
              </div>
            </div>
          ))}
          <div className="text-[10px] font-mono text-rd-dim self-end pb-1">
            BUYs held by the allowlist (doctrine evidence preserved on each intent)
          </div>
        </div>
      )}

      <div className="flex flex-wrap gap-1.5 mb-2" data-testid="buy-allowlist-symbols">
        {(al?.symbols || []).map((s) => (
          <span
            key={s}
            className="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-mono border border-rd-border text-rd-text"
            data-testid={`buy-allowlist-chip-${s.replace("/", "-")}`}
          >
            {s}
            <button
              onClick={() => remove(s)}
              disabled={busy}
              className="text-rd-dim hover:text-red-500 transition-colors"
              title={`remove ${s}`}
              data-testid={`buy-allowlist-remove-${s.replace("/", "-")}`}
            >
              <X size={10} weight="bold" />
            </button>
          </span>
        ))}
      </div>

      <div className="flex items-center gap-2 mb-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          placeholder="SOON, SOON/USD, SOONUSD — all normalize to SOON/USD"
          className="flex-1 bg-rd-bg border border-rd-border px-2 py-1 text-xs font-mono text-rd-text placeholder:text-rd-dim focus:outline-none focus:border-rd-text"
          data-testid="buy-allowlist-add-input"
        />
        <button
          onClick={add}
          disabled={busy || !input.trim()}
          className="inline-flex items-center gap-1 px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-success text-rd-success hover:bg-rd-success/10 transition-colors disabled:opacity-40"
          data-testid="buy-allowlist-add-btn"
        >
          <Plus size={10} weight="bold" /> add
        </button>
      </div>

      {elig && (
        <div className="border-t border-rd-border pt-2 mb-2" data-testid="buy-eligibility-section">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-[10px] font-mono font-bold uppercase tracking-widest text-rd-text">
              Dynamic Eligibility
            </span>
            <span
              className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-success text-rd-success"
              data-testid="buy-eligibility-mode-badge"
            >
              {elig.mode}
            </span>
            <span className="px-2 py-0.5 text-[10px] font-mono border border-amber-500 text-amber-500" data-testid="buy-eligibility-cap-badge">
              MAX ${Number(elig.max_notional_usd).toFixed(2)} / TRADE — ALL BUYS
            </span>
          </div>
          <div className="text-[10px] font-mono text-rd-dim mb-2">
            Off-pin coins pass on liquidity rules; every crypto BUY (pins included) is size-capped per trade.
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-0.5">
              <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">mode</span>
              <select
                value={elig.mode}
                onChange={(e) => setElig({ ...elig, mode: e.target.value })}
                className="bg-rd-bg border border-rd-border px-2 py-1 text-xs font-mono text-rd-text focus:outline-none focus:border-rd-text"
                data-testid="buy-eligibility-mode-select"
              >
                <option value="hybrid">hybrid</option>
                <option value="dynamic">dynamic</option>
                <option value="static">static</option>
              </select>
            </label>
            {[
              ["max_notional_usd", "max $ / trade", "buy-eligibility-max-notional"],
              ["min_dollar_vol_24h", "min 24h $ vol", "buy-eligibility-min-dvol"],
              ["max_spread_bps", "max spread bps", "buy-eligibility-max-spread"],
              ["max_pct_of_24h_vol", "% of 24h vol", "buy-eligibility-max-pct"],
            ].map(([key, label, tid]) => (
              <label key={key} className="flex flex-col gap-0.5">
                <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">{label}</span>
                <input
                  type="number"
                  step="any"
                  value={elig[key] ?? ""}
                  onChange={(e) => setElig({ ...elig, [key]: e.target.value })}
                  className={`w-24 bg-rd-bg border px-2 py-1 text-xs font-mono text-rd-text focus:outline-none focus:border-rd-text ${
                    key === "max_notional_usd" ? "border-amber-500" : "border-rd-border"
                  }`}
                  data-testid={tid}
                />
              </label>
            ))}
            <button
              onClick={saveElig}
              disabled={eligBusy}
              className="px-3 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-success text-rd-success hover:bg-rd-success/10 transition-colors disabled:opacity-40"
              data-testid="buy-eligibility-save-btn"
            >
              {eligBusy ? "saving…" : "save"}
            </button>
          </div>
          {eligMsg && (
            <div
              className={`mt-1.5 text-[10px] font-mono flex items-center gap-1 ${eligMsg.ok ? "text-rd-success" : "text-red-500"}`}
              data-testid="buy-eligibility-msg"
            >
              {!eligMsg.ok && <Warning size={10} weight="bold" />} {eligMsg.text}
            </div>
          )}
        </div>
      )}

      {lastChange && (
        <div className="text-[10px] font-mono text-rd-dim mb-2" data-testid="buy-allowlist-audit-line">
          last change: {lastChange.updated_by || "?"} · {relTime(lastChange.ts)} · previous:{" "}
          {(lastChange.previous?.symbols || []).length} pairs
          {lastChange.previous?.source === "default" ? " (default)" : ""}
        </div>
      )}

      {held?.recent_held?.length > 0 && (
        <div className="mt-2 border-t border-rd-border pt-2" data-testid="buy-allowlist-recent-held">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-1">
            recently held BUYs
          </div>
          {held.recent_held.slice(0, 6).map((r) => (
            <div key={r.intent_id} className="flex items-center gap-3 text-[10px] font-mono text-rd-muted py-0.5">
              <span className="text-rd-text w-20">{r.symbol}</span>
              <span className="w-16">{r.stack}</span>
              <span className="w-14">conf {Number(r.confidence || 0).toFixed(2)}</span>
              <span className="w-20">{r.doctrine_quality || "—"}</span>
              <span className="text-rd-dim">{relTime(r.ingest_ts)}</span>
            </div>
          ))}
        </div>
      )}

      {msg && (
        <div
          className={`mt-2 text-[10px] font-mono flex items-center gap-1 ${msg.ok ? "text-rd-success" : "text-red-500"}`}
          data-testid="buy-allowlist-msg"
        >
          {!msg.ok && <Warning size={10} weight="bold" />} {msg.text}
        </div>
      )}
    </div>
  );
};

export default BuyAllowlistPanel;
