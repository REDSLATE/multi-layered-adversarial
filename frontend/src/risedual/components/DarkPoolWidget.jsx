import React, { useEffect, useState } from "react";

/**
 * DarkPoolWidget — compact congressional / insider / corporate filings.
 *
 * Reads MC's cached dark-pool feed (/api/public/dark-pool).
 * Three-tab compact view; same auto-refresh cadence as NewsTicker.
 *
 * "Expanded" mode (prop `expanded`) is for the Signals page —
 * shows more rows, includes summaries.
 */
const REFRESH_MS = 5 * 60 * 1000;

const TABS = [
  { key: "insider",      label: "Insider (Form 4)" },
  { key: "corporate",    label: "Institutional (13F)" },
  { key: "congressional",label: "Congress" },
];

export default function DarkPoolWidget({ expanded = false, ticker = null }) {
  const [active, setActive] = useState("insider");
  const [data, setData] = useState({ insider: [], corporate: [], congressional: [] });
  const [meta, setMeta] = useState({ loaded: false, error: null });

  useEffect(() => {
    let cancelled = false;
    const limit = expanded ? 50 : 10;
    const tickerParam = ticker ? `&ticker=${encodeURIComponent(ticker)}` : "";
    const url = `${process.env.REACT_APP_BACKEND_URL}/api/public/dark-pool?type=all&limit=${limit}${tickerParam}`;

    async function load() {
      try {
        const r = await fetch(url);
        const j = await r.json();
        if (cancelled) return;
        setData({
          insider: j.insider || [],
          corporate: j.corporate || [],
          congressional: j.congressional || [],
        });
        setMeta({ loaded: true, error: null, fetched_at: j.fetched_at, counts: j.counts });
      } catch (e) {
        if (!cancelled) setMeta({ loaded: true, error: e.message });
      }
    }

    load();
    const id = setInterval(load, REFRESH_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [expanded, ticker]);

  if (!meta.loaded) {
    return (
      <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-3 text-xs text-neutral-500"
           data-testid="darkpool-loading">
        Loading institutional flows…
      </div>
    );
  }

  if (meta.error) {
    return (
      <div className="rounded-lg border border-red-900 bg-red-950/30 p-3 text-xs text-red-400"
           data-testid="darkpool-error">
        Dark pool feed offline — {meta.error}
      </div>
    );
  }

  const activeItems = data[active] || [];

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-950" data-testid="darkpool-widget">
      <div className="flex items-center justify-between border-b border-neutral-800 px-3 py-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">
          {ticker ? `${ticker} · Smart Money` : "Smart Money Flow"}
        </h3>
        <span className="text-[10px] text-neutral-600">auto-refresh 5m</span>
      </div>

      <div className="flex border-b border-neutral-800 text-xs">
        {TABS.map((t) => {
          const count = data[t.key]?.length || 0;
          const isActive = active === t.key;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => setActive(t.key)}
              data-testid={`darkpool-tab-${t.key}`}
              className={`flex-1 px-2 py-2 transition-colors ${
                isActive
                  ? "bg-neutral-900 text-amber-400 border-b-2 border-amber-500"
                  : "text-neutral-500 hover:text-neutral-300"
              }`}
            >
              {t.label}
              <span className="ml-1 text-[10px] text-neutral-600">({count})</span>
            </button>
          );
        })}
      </div>

      <ul className={`divide-y divide-neutral-900 overflow-y-auto ${expanded ? "max-h-[600px]" : "max-h-64"}`}>
        {activeItems.length === 0 && (
          <li className="px-3 py-4 text-center text-xs text-neutral-600">
            {ticker ? `No ${active} activity for ${ticker} yet.` : "No data yet — first fetch in progress."}
          </li>
        )}
        {activeItems.map((it, i) => (
          <li key={`${active}-${i}`} className="px-3 py-2 hover:bg-neutral-900/40">
            <DarkPoolRow item={it} kind={active} expanded={expanded} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function DarkPoolRow({ item, kind, expanded }) {
  if (kind === "congressional") {
    const tx = String(item.transaction || "").toUpperCase();
    const tone = tx.includes("PURCHASE") || tx.includes("BUY") ? "text-emerald-400"
               : tx.includes("SALE") || tx.includes("SELL") ? "text-red-400"
               : "text-neutral-400";
    return (
      <div className="text-sm text-neutral-200" data-testid="dp-row-congress">
        <div className="flex items-baseline gap-2">
          <span className="text-[10px] uppercase font-mono text-neutral-500 shrink-0">
            {item.chamber}
          </span>
          <span className="font-medium">{item.representative || "—"}</span>
          {item.party && <span className="text-[10px] text-neutral-600">({item.party})</span>}
        </div>
        <div className="mt-0.5 text-xs">
          <span className={`font-medium ${tone}`}>{item.transaction || "?"}</span>
          {" "}
          <span className="text-amber-400 font-mono">{item.ticker || ""}</span>
          {item.amount && <span className="text-neutral-500"> · {item.amount}</span>}
          {item.transaction_date && <span className="text-neutral-600"> · {item.transaction_date}</span>}
        </div>
      </div>
    );
  }

  // insider / corporate share the EDGAR atom shape
  return (
    <div className="text-sm" data-testid={`dp-row-${kind}`}>
      <a
        href={item.link}
        target="_blank"
        rel="noopener noreferrer"
        className="text-neutral-200 hover:text-white"
      >
        <div className="flex items-baseline gap-2">
          <span className="text-[10px] uppercase font-mono text-amber-500/80 shrink-0">
            {item.form_type || "?"}
          </span>
          <span className="line-clamp-1">{item.filer || item.title}</span>
        </div>
        {expanded && item.summary && (
          <p className="mt-1 text-xs text-neutral-500 line-clamp-2">{item.summary}</p>
        )}
        {item.filed_at && (
          <span className="text-[10px] text-neutral-600">{item.filed_at}</span>
        )}
      </a>
    </div>
  );
}
