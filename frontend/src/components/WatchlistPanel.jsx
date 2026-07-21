import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning, X, Plus, ArrowsClockwise } from "@phosphor-icons/react";

/** Watchlist (Operator Pins) — pin symbols into every universe
 *  refresh + throttle screener penny-stock noise at runtime. */
export default function WatchlistPanel() {
  const [data, setData] = useState(null);
  const [input, setInput] = useState("");
  const [lane, setLane] = useState("equity");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [minPrice, setMinPrice] = useState("");
  const [admitCap, setAdmitCap] = useState("");

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get("/admin/universe/watchlist");
      setData(d);
      setMinPrice(d.quality?.min_price_equity ?? "");
      setAdmitCap(d.quality?.screener_admit_cap ?? "");
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const run = async (fn, okText) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await fn();
      setMsg({ ok: true, text: okText(r) });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  const add = () => {
    const sym = input.trim();
    if (!sym) return;
    run(
      () => api.post("/admin/universe/watchlist", { symbol: sym, lane }),
      (r) => { setInput(""); return `${r.data.symbol} pinned to ${r.data.lane} — hit REFRESH NOW or wait ≤15min`; },
    );
  };

  const remove = (sym, l) => run(
    () => api.delete(`/admin/universe/watchlist/${l}/${encodeURIComponent(sym)}`),
    () => `${sym} unpinned`,
  );

  const applyQuality = () => run(
    () => api.post("/admin/universe/quality", {
      min_price_equity: minPrice === "" ? null : Number(minPrice),
      screener_admit_cap: admitCap === "" ? null : Number(admitCap),
    }),
    () => "quality knobs saved — takes effect next refresh",
  );

  const refreshNow = () => run(
    () => api.post("/admin/universe/refresh"),
    (r) => {
      const l = r.data.lanes || {};
      return `universe rebuilt — equity ${l.equity?.final ?? l.equity?.skipped ?? "?"} · crypto ${l.crypto?.final ?? "?"}`;
    },
  );

  if (!data) return null;

  const comp = data.composition || {};
  const pinRows = [
    ...(data.pins?.equity || []).map((p) => ({ ...p, lane: "equity" })),
    ...(data.pins?.crypto || []).map((p) => ({ ...p, lane: "crypto" })),
  ];

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="watchlist-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
          Watchlist · Operator Pins — {pinRows.length} pinned
        </div>
        <button
          onClick={refreshNow}
          disabled={busy}
          data-testid="watchlist-refresh-now"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40 flex items-center gap-1"
        >
          <ArrowsClockwise size={10} />refresh now
        </button>
      </div>

      <div className="text-[10px] font-mono text-rd-dim mb-2" data-testid="watchlist-composition">
        universe: equity {comp.equity?.total ?? "?"} ({comp.equity?.pinned ?? 0} pinned
        {comp.equity?.under_4 != null ? ` · ${comp.equity.under_4} under $4` : ""})
        {" · "}crypto {comp.crypto?.total ?? "?"} ({comp.crypto?.pinned ?? 0} pinned)
      </div>

      <div className="flex items-center gap-2">
        <div className="flex border border-rd-border">
          {["equity", "crypto"].map((l) => (
            <button
              key={l}
              onClick={() => setLane(l)}
              data-testid={`watchlist-lane-${l}`}
              className={`text-[10px] font-mono uppercase tracking-widest px-2 py-1 ${lane === l ? "bg-rd-text text-black" : "text-rd-dim hover:text-rd-text"}`}
            >
              {l}
            </button>
          ))}
        </div>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && add()}
          placeholder={lane === "equity" ? "e.g. AAPL" : "e.g. BTC or BTC/USD"}
          className="flex-1 bg-transparent border border-rd-border px-2 py-1 text-xs font-mono text-rd-text placeholder:text-rd-dim focus:outline-none focus:border-rd-text"
          data-testid="watchlist-input"
        />
        <button
          onClick={add}
          disabled={busy || !input.trim()}
          data-testid="watchlist-add"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
        >
          <Plus size={10} />pin
        </button>
      </div>

      {msg && (
        <div
          className="mt-2 px-2 py-1 text-[10px] font-mono border"
          style={{ color: msg.ok ? "#10B981" : "#EF4444", borderColor: msg.ok ? "#10B981" : "#EF4444" }}
          data-testid="watchlist-msg"
        >
          {!msg.ok && <Warning size={10} className="inline mr-1" />}{msg.text}
        </div>
      )}

      {pinRows.length > 0 && (
        <div className="mt-2 border-t border-rd-border/50 pt-2 flex flex-wrap gap-1" data-testid="watchlist-pins">
          {pinRows.map((p) => (
            <span
              key={`${p.lane}-${p.symbol}`}
              className="text-[10px] font-mono border border-rd-border px-1.5 py-0.5 flex items-center gap-1 text-rd-text"
            >
              {p.symbol}
              <span className="text-rd-dim">{p.lane === "crypto" ? "cr" : "eq"}</span>
              <button
                onClick={() => remove(p.symbol, p.lane)}
                disabled={busy}
                data-testid={`watchlist-remove-${p.symbol.split("/")[0]}`}
                className="text-rd-dim hover:text-rd-danger"
                title="Unpin"
              >
                <X size={10} />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="mt-2 border-t border-rd-border/50 pt-2 flex items-center gap-3 flex-wrap">
        <span className="text-[9px] uppercase tracking-widest text-rd-dim font-mono">
          screener quality:
        </span>
        <label className="text-[10px] font-mono text-rd-dim flex items-center gap-1">
          min equity price $
          <input
            value={minPrice}
            onChange={(e) => setMinPrice(e.target.value)}
            placeholder="1"
            className="w-14 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text"
            data-testid="watchlist-min-price"
          />
        </label>
        <label className="text-[10px] font-mono text-rd-dim flex items-center gap-1">
          max screener admits
          <input
            value={admitCap}
            onChange={(e) => setAdmitCap(e.target.value)}
            placeholder="50"
            className="w-14 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text"
            data-testid="watchlist-admit-cap"
          />
        </label>
        <button
          onClick={applyQuality}
          disabled={busy}
          data-testid="watchlist-quality-apply"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40"
        >
          apply
        </button>
      </div>

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Pins enter every universe refresh first — they bypass hysteresis and quality filters, so brains
        always evaluate them. Raise min price to kill penny-pump noise; set max screener admits to 0 for a
        pins-only universe. Crypto pins need a Kraken pair mapping first. Knobs apply next refresh (≤15min) or hit REFRESH NOW.
      </div>
    </div>
  );
}
