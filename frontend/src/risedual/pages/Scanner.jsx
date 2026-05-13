import React, { useEffect, useState } from "react";
import { useTier } from "../context/TierContext";
import { mc, fmtAgo } from "../lib/mc";
import { Search, Zap, TrendingUp, TrendingDown } from "lucide-react";

const SIGNAL_CLS = {
  bullish: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  bearish: "border-rose-500/30 bg-rose-500/10 text-rose-300",
  neutral: "border-slate-500 bg-zinc-800/40 text-zinc-300",
};

const CATEGORY_LABEL = {
  momentum: "Momentum",
  trend: "Trend",
  volatility: "Volatility",
  volume: "Volume",
  mean_reversion: "Mean Reversion",
};

function PresetCard({ preset, active, onSelect }) {
  const cls = SIGNAL_CLS[preset.signal] || SIGNAL_CLS.neutral;
  return (
    <button
      onClick={() => onSelect(preset.preset_id)}
      data-testid={`rd-preset-${preset.preset_id}`}
      className={
        "group w-full rounded-lg border p-4 text-left transition-colors " +
        (active
          ? "border-emerald-500/50 bg-emerald-500/5"
          : "border-slate-700 bg-slate-800/40 hover:border-slate-600")
      }
    >
      <div className="flex items-start justify-between gap-2">
        <div className="font-display text-[14px] text-white">{preset.name}</div>
        <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.14em] ${cls}`}>
          {preset.signal}
        </span>
      </div>
      <div className="mt-1 font-mono text-[10px] uppercase tracking-[0.16em] text-zinc-500">
        {CATEGORY_LABEL[preset.category] || preset.category}
      </div>
    </button>
  );
}

function StrengthBar({ value }) {
  return (
    <div className="h-1 w-24 overflow-hidden rounded-full bg-slate-700/60">
      <div
        className="h-full bg-emerald-400"
        style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  );
}

export default function Scanner() {
  const { tier } = useTier();
  const [presets, setPresets] = useState({ loading: true, list: [] });
  const [activePreset, setActivePreset] = useState(null);
  const [scan, setScan] = useState({ loading: false, data: null, error: null });

  useEffect(() => {
    let cancelled = false;
    mc.scannerPresets(tier).then((r) => {
      if (cancelled) return;
      if (r.ok) {
        setPresets({ loading: false, list: r.data.presets || [] });
        if (!activePreset && r.data.presets?.length) {
          setActivePreset(r.data.presets[0].preset_id);
        }
      } else {
        setPresets({ loading: false, list: [] });
      }
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tier]);

  useEffect(() => {
    if (!activePreset) return;
    let cancelled = false;
    setScan({ loading: true, data: null, error: null });
    mc.scannerScan(tier, activePreset).then((r) => {
      if (cancelled) return;
      r.ok
        ? setScan({ loading: false, data: r.data, error: null })
        : setScan({ loading: false, data: null, error: r.detail });
    });
    return () => { cancelled = true; };
  }, [tier, activePreset]);

  return (
    <div className="space-y-8" data-testid="rd-scanner-page">
      <div>
        <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
          Pattern Scanner
        </div>
        <h1 className="mt-2 font-display text-3xl tracking-tight text-white md:text-4xl">
          Ten setups, scanned live.
        </h1>
        <p className="mt-3 max-w-xl text-[14px] text-zinc-400">
          Classic technical patterns — MACD crosses, Bollinger squeezes, RSI extremes,
          52-week breakouts — detected across MC's covered tape in real time.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-[300px_1fr]">
        {/* PRESET LIST */}
        <div className="space-y-2" data-testid="rd-preset-list">
          {presets.loading ? (
            <div className="rounded-lg border border-slate-700 bg-slate-800/40 p-6 text-center font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">
              Loading presets…
            </div>
          ) : (
            presets.list.map((p) => (
              <PresetCard
                key={p.preset_id}
                preset={p}
                active={p.preset_id === activePreset}
                onSelect={setActivePreset}
              />
            ))
          )}
        </div>

        {/* RESULTS */}
        <div className="space-y-4">
          {scan.loading && (
            <div
              data-testid="rd-scan-loading"
              className="rounded-lg border border-slate-700 bg-slate-800/40 p-12 text-center font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-600"
            >
              <Zap size={14} className="mx-auto mb-2 animate-pulse text-emerald-400" />
              Scanning the tape…
            </div>
          )}

          {scan.error && (
            <div data-testid="rd-scan-error" className="rounded-lg border border-rose-900/40 bg-rose-950/30 p-6 text-[13px] text-rose-300">
              {scan.error}
            </div>
          )}

          {scan.data && (
            <>
              <div className="rounded-lg border border-slate-700 bg-slate-800/40 p-5" data-testid="rd-scan-summary">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="font-display text-lg text-white">{scan.data.name}</div>
                    <div className="mt-0.5 font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
                      {CATEGORY_LABEL[scan.data.category] || scan.data.category} · {scan.data.signal}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="font-display text-2xl text-emerald-300">
                      {scan.data.matched}
                    </div>
                    <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-zinc-500">
                      of {scan.data.scanned} scanned
                    </div>
                  </div>
                </div>
              </div>

              {(scan.data.matches || []).length === 0 ? (
                <div
                  data-testid="rd-scan-empty"
                  className="rounded-lg border border-slate-700 bg-slate-800/40 p-10 text-center text-[13px] text-zinc-500"
                >
                  <Search size={16} className="mx-auto mb-3 text-zinc-600" />
                  No symbols match this pattern right now.
                </div>
              ) : (
                <div className="overflow-hidden rounded-lg border border-slate-700" data-testid="rd-scan-matches">
                  <table className="w-full text-left text-[13px]">
                    <thead className="bg-slate-800/60">
                      <tr className="font-mono text-[10px] uppercase tracking-[0.16em] text-zinc-500">
                        <th className="px-4 py-3">Symbol</th>
                        <th className="px-4 py-3">Strength</th>
                        <th className="px-4 py-3">Detail</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-700">
                      {scan.data.matches.map((m) => (
                        <tr key={m.symbol} data-testid={`rd-scan-match-${m.symbol}`} className="hover:bg-slate-800/50">
                          <td className="px-4 py-3 font-display text-white">{m.symbol}</td>
                          <td className="px-4 py-3">
                            <div className="flex items-center gap-3">
                              <StrengthBar value={m.strength} />
                              <span className="font-mono text-[11px] text-zinc-300">{m.strength}</span>
                            </div>
                          </td>
                          <td className="px-4 py-3 text-[12px] text-zinc-400">{m.detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
