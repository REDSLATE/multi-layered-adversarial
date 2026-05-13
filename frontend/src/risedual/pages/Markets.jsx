import React, { useEffect, useState } from "react";
import { useTier } from "../context/TierContext";
import { Search } from "lucide-react";
import CandleChart from "../components/CandleChart";

function _kind(symbol) {
  // Naive classifier: anything with '/' is a crypto pair.
  if (symbol.includes("/")) return "crypto";
  if (/^[A-Z]{1,5}$/.test(symbol)) return "stock";
  return "other";
}

const KIND_LABEL = { crypto: "Crypto", stock: "Stock", other: "Other" };

export default function Markets() {
  const { tier } = useTier();
  const [items, setItems] = useState({ loading: true, list: [], error: null });
  const [active, setActive] = useState(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    setItems({ loading: true, list: [], error: null });
    fetch(`${process.env.REACT_APP_BACKEND_URL}/api/public/bars`, {
      headers: {
        "X-RiseDual-Token": process.env.REACT_APP_RISEDUAL_TOKEN || "",
        "X-RiseDual-User-Tier": tier || "free",
      },
    })
      .then(async (r) => {
        const data = await r.json().catch(() => null);
        if (cancelled) return;
        if (!r.ok) {
          setItems({ loading: false, list: [], error: data?.detail || `HTTP ${r.status}` });
          return;
        }
        const list = data.items || [];
        setItems({ loading: false, list, error: null });
        // Prefer crypto first by default, since those have real data.
        if (list.length && !active) {
          const crypto = list.find((x) => _kind(x.symbol) === "crypto");
          setActive((crypto || list[0]).symbol);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setItems({ loading: false, list: [], error: e.message });
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tier]);

  const filtered = (items.list || []).filter((x) =>
    !filter ? true : x.symbol.toLowerCase().includes(filter.toLowerCase()),
  );
  const grouped = filtered.reduce((acc, x) => {
    const k = _kind(x.symbol);
    acc[k] = acc[k] || [];
    acc[k].push(x);
    return acc;
  }, {});
  // Display order: crypto first (real data), then stocks, then everything else.
  const GROUP_ORDER = ["crypto", "stock", "other"];
  const orderedGroups = GROUP_ORDER
    .filter((k) => (grouped[k] || []).length > 0)
    .map((k) => [k, grouped[k]]);

  return (
    <div className="space-y-8" data-testid="rd-markets-page">
      <div>
        <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-slate-500">
          Markets
        </div>
        <h1 className="mt-2 font-display text-3xl tracking-tight text-white md:text-4xl">
          Stocks & crypto, on candles.
        </h1>
        <p className="mt-3 max-w-xl text-[14px] text-slate-400">
          Every symbol MC has bars for. Pick one to load candles across timeframes.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[280px_1fr]">
        {/* SYMBOL PICKER */}
        <div className="space-y-3" data-testid="rd-markets-picker">
          <div className="relative">
            <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter symbols…"
              data-testid="rd-markets-filter"
              className="w-full rounded-md border border-slate-700 bg-slate-800/40 px-9 py-2 text-[13px] text-slate-100 placeholder-slate-500 focus:border-emerald-500/50 focus:outline-none"
            />
          </div>

          {items.loading && (
            <div className="rounded-lg border border-slate-700 bg-slate-800/40 p-6 text-center font-mono text-[11px] uppercase tracking-[0.18em] text-slate-600">
              Loading symbols…
            </div>
          )}
          {items.error && (
            <div className="rounded-lg border border-rose-900/40 bg-rose-950/30 p-4 text-[12px] text-rose-300">
              {items.error}
            </div>
          )}

          {orderedGroups.map(([kind, list]) => (
            <div key={kind} className="space-y-1">
              <div className="px-1 font-mono text-[10px] uppercase tracking-[0.18em] text-slate-500">
                {KIND_LABEL[kind] || kind} · {list.length}
              </div>
              <div className="space-y-1">
                {list.slice(0, 30).map((x) => {
                  const isActive = x.symbol === active;
                  return (
                    <button
                      key={x.symbol}
                      onClick={() => setActive(x.symbol)}
                      data-testid={`rd-markets-symbol-${x.symbol}`}
                      className={
                        "block w-full rounded-md border px-3 py-2 text-left transition-colors " +
                        (isActive
                          ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-100"
                          : "border-slate-700 bg-slate-800/40 text-slate-200 hover:border-slate-500")
                      }
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-display text-[13px]">{x.symbol}</span>
                        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
                          {x.tfs.join(" · ")}
                        </span>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}

          {!items.loading && !items.error && filtered.length === 0 && (
            <div className="rounded-lg border border-slate-700 bg-slate-800/40 p-6 text-center text-[12px] text-slate-500">
              No matches.
            </div>
          )}
        </div>

        {/* CANDLE PANEL */}
        <div data-testid="rd-markets-chart-wrap">
          {active ? (
            <CandleChart symbol={active} />
          ) : (
            <div className="rounded-xl border border-slate-700 bg-slate-800/40 p-12 text-center text-[13px] text-slate-500">
              Pick a symbol from the list to load candles.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
