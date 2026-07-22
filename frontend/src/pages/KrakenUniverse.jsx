import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning, PushPin, ArrowsClockwise } from "@phosphor-icons/react";

const QUOTES = ["USD", "USDT", "USDC", "EUR", "BTC", "ETH"];

/** Kraken Universe — every online pair, grouped by quote, ranked by
 *  24h notional. Pin USD pairs straight into the trading universe. */
export default function KrakenUniverse() {
  const [data, setData] = useState(null);
  const [quote, setQuote] = useState("USD");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async (force = false) => {
    setBusy(true);
    try {
      const { data: d } = await api.get("/admin/kraken-universe", {
        params: { quote, limit: 150, q: q || undefined, force: force || undefined },
      });
      setData(d);
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  }, [quote, q]);

  useEffect(() => { load(); }, [load]);

  const pin = async (base) => {
    setBusy(true);
    setMsg(null);
    try {
      await api.post("/admin/universe/watchlist", { symbol: `${base}/USD`, lane: "crypto" });
      setMsg({ ok: true, text: `${base}/USD pinned to crypto universe — refresh applies ≤15min` });
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  const fmt = (n) => {
    if (n == null) return "—";
    if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
    if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
    if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
    return `$${Number(n).toFixed(2)}`;
  };

  return (
    <div data-testid="kraken-universe-page">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h1 className="text-lg font-mono font-bold text-rd-text uppercase tracking-widest">Kraken Universe</h1>
          <div className="text-[10px] font-mono text-rd-dim">
            {data ? `${data.total_online_pairs} online pairs · built ${String(data.built_at).slice(5, 16).replace("T", " ")}Z · auto-rebuilds every 6h` : "loading…"}
          </div>
        </div>
        <button onClick={() => load(true)} disabled={busy} data-testid="kraken-universe-rebuild"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40 flex items-center gap-1">
          <ArrowsClockwise size={10} />rebuild now
        </button>
      </div>

      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <div className="flex border border-rd-border">
          {QUOTES.map((qt) => (
            <button key={qt} onClick={() => setQuote(qt)} data-testid={`kraken-universe-quote-${qt}`}
              className={`text-[10px] font-mono uppercase tracking-widest px-2 py-1 ${quote === qt ? "bg-rd-text text-black" : "text-rd-dim hover:text-rd-text"}`}>
              {qt}{data?.counts_by_quote?.[qt] ? ` (${data.counts_by_quote[qt]})` : ""}
            </button>
          ))}
        </div>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search base…"
          data-testid="kraken-universe-search"
          className="bg-transparent border border-rd-border px-2 py-1 text-xs font-mono text-rd-text placeholder:text-rd-dim focus:outline-none focus:border-rd-text w-40" />
      </div>

      {msg && (
        <div className="mb-2 px-2 py-1 text-[10px] font-mono border inline-block"
          style={{ color: msg.ok ? "#10B981" : "#EF4444", borderColor: msg.ok ? "#10B981" : "#EF4444" }}
          data-testid="kraken-universe-msg">
          {!msg.ok && <Warning size={10} className="inline mr-1" />}{msg.text}
        </div>
      )}

      {data && (
        <div className="border border-rd-border" data-testid="kraken-universe-table">
          <div className="grid grid-cols-[44px_110px_1fr_1fr_1fr_80px_70px_60px] gap-1 text-[9px] uppercase tracking-widest text-rd-dim font-mono px-2 py-1.5 border-b border-rd-border">
            <span>#</span><span>pair</span><span>last</span><span>24h notional</span><span>min order</span><span>affordable</span><span>mapped</span><span></span>
          </div>
          {data.rows.map((r) => (
            <div key={r.pair}
              className="grid grid-cols-[44px_110px_1fr_1fr_1fr_80px_70px_60px] gap-1 text-[10px] font-mono px-2 py-1 items-center border-b border-rd-border/30 hover:bg-rd-border/10"
              data-testid={`kraken-universe-row-${r.base}`}>
              <span className="text-rd-dim">{r.rank}</span>
              <span className="font-bold text-rd-text">{r.wsname}</span>
              <span className="text-rd-dim">{r.last < 0.01 ? r.last.toFixed(8) : r.last.toFixed(r.last < 1 ? 5 : 2)}</span>
              <span className="text-rd-text">{fmt(r.notional_24h)}</span>
              <span className="text-rd-dim">{r.min_order_quote != null ? fmt(r.min_order_quote) : "—"}</span>
              <span>
                {r.affordable === null ? <span className="text-rd-dim">n/a</span>
                  : r.affordable ? <span className="text-emerald-500">yes</span>
                    : <span className="text-rd-danger">&gt;${data.per_order_cap_usd}</span>}
              </span>
              <span>
                {r.mapped === null ? <span className="text-rd-dim">—</span>
                  : r.mapped ? <span className="text-emerald-500">yes</span>
                    : <span className="text-amber-500">auto</span>}
              </span>
              <span>
                {r.quote === "USD" && (
                  <button onClick={() => pin(r.base)} disabled={busy}
                    data-testid={`kraken-universe-pin-${r.base}`}
                    title="Pin to crypto trading universe"
                    className="text-[9px] font-mono uppercase border border-rd-border hover:border-rd-text px-1 py-0.5 disabled:opacity-40 flex items-center gap-0.5">
                    <PushPin size={9} />pin
                  </button>
                )}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Ranked by 24h notional (volume × vwap). "Mapped: auto" pairs get mapped automatically the moment they enter
        the trading universe (movers or pins). "Affordable" compares Kraken's minimum order (ordermin × last) against
        the per-order cap — unaffordable pairs are excluded from the trading universe automatically.
      </div>
    </div>
  );
}
