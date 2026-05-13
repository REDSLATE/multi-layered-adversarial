import React, { useEffect, useState } from "react";
import { useTier } from "../context/TierContext";
import { mc } from "../lib/mc";
import { TrendingUp, TrendingDown } from "lucide-react";

const BAND_CLS = {
  strong_buy: "bg-emerald-500/30 border-emerald-400/40 text-emerald-200",
  mild_buy: "bg-emerald-500/15 border-emerald-500/30 text-emerald-300",
  neutral: "bg-zinc-900 border-zinc-800 text-zinc-400",
  mild_sell: "bg-rose-500/15 border-rose-500/30 text-rose-300",
  strong_sell: "bg-rose-500/30 border-rose-400/40 text-rose-200",
};

function HeatCell({ row }) {
  const cls = BAND_CLS[row.color_band] || BAND_CLS.neutral;
  const pct = row.change_24h_pct;
  return (
    <div
      data-testid={`rd-heat-cell-${row.symbol}`}
      className={`rounded-md border p-3 transition-transform hover:scale-[1.02] ${cls}`}
    >
      <div className="font-display text-sm">{row.symbol}</div>
      <div className="mt-1 font-mono text-[13px]">
        {pct > 0 ? "+" : ""}{pct.toFixed(2)}%
      </div>
    </div>
  );
}

function SectorRow({ s }) {
  const cls = BAND_CLS[s.color_band] || BAND_CLS.neutral;
  const isLive = s.coverage === "live";
  return (
    <div
      data-testid={`rd-sector-row-${s.symbol}`}
      className={`flex items-center justify-between rounded-md border px-4 py-3 ${cls}`}
    >
      <div className="flex items-center gap-3">
        <span className="font-display text-[13px]">{s.symbol}</span>
        <span className="font-mono text-[11px] uppercase tracking-[0.12em] opacity-70">
          {s.name}
        </span>
      </div>
      <div className="font-mono text-[13px]">
        {isLive
          ? `${s.change_24h_pct > 0 ? "+" : ""}${s.change_24h_pct.toFixed(2)}%`
          : <span className="text-[10px] uppercase tracking-[0.16em] opacity-60">no coverage</span>
        }
      </div>
    </div>
  );
}

export default function Heatmap() {
  const { tier } = useTier();
  const [heat, setHeat] = useState({ loading: true, data: null, error: null });
  const [sectors, setSectors] = useState({ loading: true, data: null });

  useEffect(() => {
    let cancelled = false;
    setHeat({ loading: true, data: null, error: null });
    setSectors({ loading: true, data: null });
    mc.heatmap(tier).then((r) => {
      if (cancelled) return;
      r.ok
        ? setHeat({ loading: false, data: r.data, error: null })
        : setHeat({ loading: false, data: null, error: r.detail });
    });
    mc.sectors(tier).then((r) => {
      if (cancelled) return;
      setSectors({ loading: false, data: r.ok ? r.data : null });
    });
    return () => { cancelled = true; };
  }, [tier]);

  const items = heat.data?.items || [];
  const gainers = items.filter((r) => r.change_24h_pct > 0).length;
  const losers = items.filter((r) => r.change_24h_pct < 0).length;

  return (
    <div className="space-y-10" data-testid="rd-heatmap-page">
      <div>
        <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
          24h Heatmap
        </div>
        <h1 className="mt-2 font-display text-3xl tracking-tight text-white md:text-4xl">
          The tape, at a glance.
        </h1>
      </div>

      {/* SUMMARY */}
      <section className="grid grid-cols-2 gap-4 md:grid-cols-3" data-testid="rd-heatmap-summary">
        <div className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-5">
          <div className="mb-3 inline-flex h-8 w-8 items-center justify-center rounded-md bg-emerald-500/10 text-emerald-400">
            <TrendingUp size={16} strokeWidth={1.8} />
          </div>
          <div className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">Gainers</div>
          <div className="mt-1 font-display text-2xl text-emerald-300">{gainers}</div>
        </div>
        <div className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-5">
          <div className="mb-3 inline-flex h-8 w-8 items-center justify-center rounded-md bg-rose-500/10 text-rose-400">
            <TrendingDown size={16} strokeWidth={1.8} />
          </div>
          <div className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">Decliners</div>
          <div className="mt-1 font-display text-2xl text-rose-300">{losers}</div>
        </div>
        <div className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-5 md:col-span-1 col-span-2">
          <div className="mb-3 h-8 w-8" />
          <div className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">Symbols covered</div>
          <div className="mt-1 font-display text-2xl text-white">{items.length}</div>
        </div>
      </section>

      {/* HEATMAP GRID */}
      <section data-testid="rd-heatmap-grid-section">
        <div className="mb-4">
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">By symbol</div>
          <h2 className="mt-1 font-display text-xl text-white">24-hour change</h2>
        </div>
        {heat.loading && (
          <div className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-12 text-center font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-600">
            Loading tape…
          </div>
        )}
        {heat.error && (
          <div className="rounded-lg border border-rose-900/40 bg-rose-950/30 p-6 text-[13px] text-rose-300">
            {heat.error}
          </div>
        )}
        {heat.data && items.length === 0 && (
          <div data-testid="rd-heatmap-empty" className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-10 text-center text-[13px] text-zinc-500">
            No 24h coverage yet. Once feeders have 24h of bars the grid populates.
          </div>
        )}
        {items.length > 0 && (
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8" data-testid="rd-heatmap-grid">
            {items.map((r) => (
              <HeatCell key={r.symbol} row={r} />
            ))}
          </div>
        )}
      </section>

      {/* SECTORS */}
      <section data-testid="rd-sectors-section">
        <div className="mb-4">
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">Sector rotation</div>
          <h2 className="mt-1 font-display text-xl text-white">SPDR sectors · 24h</h2>
        </div>
        {sectors.loading && (
          <div className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-8 text-center font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-600">
            Loading sectors…
          </div>
        )}
        {sectors.data && (
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2" data-testid="rd-sectors-grid">
            {(sectors.data.items || []).map((s) => (
              <SectorRow key={s.symbol} s={s} />
            ))}
          </div>
        )}
        {sectors.data?.degraded && (
          <div className="mt-3 text-[11px] font-mono uppercase tracking-[0.18em] text-zinc-600">
            Sector feeders not wired yet · auto-populates when ETF data flows
          </div>
        )}
      </section>
    </div>
  );
}
