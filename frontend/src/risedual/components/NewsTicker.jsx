import React, { useEffect, useState } from "react";

/**
 * NewsTicker — compact, auto-refreshing market headlines strip.
 *
 * Reads MC's cached news (/api/public/news). MC handles upstream
 * source aggregation and caching; this component is render-only.
 *
 * Refreshes every 5 minutes. Public, no auth.
 */
const REFRESH_MS = 5 * 60 * 1000;
const VISIBLE_LIMIT = 15;

export default function NewsTicker() {
  const [items, setItems] = useState([]);
  const [meta, setMeta] = useState({ loaded: false, error: null });

  useEffect(() => {
    let cancelled = false;
    const url = `${process.env.REACT_APP_BACKEND_URL}/api/public/news?limit=${VISIBLE_LIMIT}`;

    async function load() {
      try {
        const r = await fetch(url);
        const data = await r.json();
        if (cancelled) return;
        setItems(data.items || []);
        setMeta({ loaded: true, error: null, fetched_at: data.fetched_at });
      } catch (e) {
        if (!cancelled) setMeta({ loaded: true, error: e.message });
      }
    }

    load();
    const id = setInterval(load, REFRESH_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (!meta.loaded) {
    return (
      <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-3 text-xs text-neutral-500"
           data-testid="news-ticker-loading">
        Loading market news…
      </div>
    );
  }

  if (meta.error) {
    return (
      <div className="rounded-lg border border-red-900 bg-red-950/30 p-3 text-xs text-red-400"
           data-testid="news-ticker-error">
        News feed offline — {meta.error}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-950" data-testid="news-ticker">
      <div className="flex items-center justify-between border-b border-neutral-800 px-3 py-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">
          Market News
        </h3>
        <span className="text-[10px] text-neutral-600">
          {items.length} headlines · auto-refresh 5m
        </span>
      </div>
      <ul className="max-h-72 divide-y divide-neutral-900 overflow-y-auto">
        {items.map((a, i) => (
          <li key={`${a.link}-${i}`} className="px-3 py-2 hover:bg-neutral-900/40 transition-colors">
            <a
              href={a.link}
              target="_blank"
              rel="noopener noreferrer"
              className="block text-sm text-neutral-200 hover:text-white"
              data-testid={`news-item-${i}`}
            >
              <div className="flex items-baseline gap-2">
                <span className="text-[10px] font-mono uppercase text-amber-500/80 shrink-0">
                  {a.source}
                </span>
                <span className="line-clamp-2 leading-snug">{a.title}</span>
              </div>
            </a>
          </li>
        ))}
        {items.length === 0 && (
          <li className="px-3 py-4 text-center text-xs text-neutral-600">
            No headlines yet — first fetch in progress.
          </li>
        )}
      </ul>
    </div>
  );
}
