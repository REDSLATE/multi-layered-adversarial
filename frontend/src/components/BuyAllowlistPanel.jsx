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

  const load = useCallback(async () => {
    try {
      const [{ data: a }, { data: h }] = await Promise.all([
        api.get("/admin/universe/crypto-buy-allowlist"),
        api.get("/admin/universe/crypto-buy-allowlist/held-stats"),
      ]);
      setAl(a.allowlist);
      setAudit(a.audit || []);
      setHeld(h);
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

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
