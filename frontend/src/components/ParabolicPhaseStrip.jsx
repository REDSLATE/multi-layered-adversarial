import React, { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui-bits";
import {
  Lightning, Sparkle, TrendUp, TrendDown, ArrowsClockwise,
} from "@phosphor-icons/react";

/**
 * Parabolic phase strip — shows the brains' live read of the equity
 * universe through the PAVS-style spike/fade lens.
 *
 * Phase legend:
 *   🟢 accumulation — early run, healthy expansion (full size)
 *   🟡 parabolic — extended, RVOL accelerating (half size auto)
 *   🔴 topping — 2 red bars after green run (no new longs)
 *   ⚫ fade — broken below recent highs (exit-only)
 *
 * Backs onto:
 *   GET /api/admin/parabolic/phases
 */
const PHASE_META = {
  accumulation: { label: "Accumulating", color: "emerald", Icon: TrendUp },
  parabolic:    { label: "Parabolic",    color: "amber",   Icon: Lightning },
  topping:      { label: "Topping",      color: "rose",    Icon: TrendDown },
  fade:         { label: "Fading",       color: "zinc",    Icon: TrendDown },
};

export default function ParabolicPhaseStrip() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await api.get("/api/admin/parabolic/phases");
      setData(res.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || "fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = () => { if (alive) load(); };
    tick();
    const t = setInterval(tick, 15_000);
    return () => { alive = false; clearInterval(t); };
  }, [load]);

  const counts = data?.counts || {};
  const symbols = data?.symbols || {};

  return (
    <Card data-testid="parabolic-phase-strip" className="p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Sparkle size={18} weight="duotone" className="text-amber-400" />
          <h3 className="text-sm font-semibold tracking-wide">
            Parabolic Phase Map — Equity Universe
          </h3>
        </div>
        <button
          data-testid="parabolic-phase-refresh"
          onClick={load}
          disabled={loading}
          className="h-7 px-2 opacity-60 hover:opacity-100 transition"
        >
          <ArrowsClockwise size={14} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {err && (
        <div className="text-xs text-rose-400 mb-2" data-testid="parabolic-phase-error">
          {err}
        </div>
      )}

      {!data && !err && <div className="text-xs opacity-60">Reading the tape…</div>}

      {data && (
        <>
          <div className="grid grid-cols-4 gap-2 mb-3">
            {Object.entries(PHASE_META).map(([key, meta]) => {
              const n = counts[key] || 0;
              const Icon = meta.Icon;
              const colorClasses = {
                emerald: "border-emerald-500/30 bg-emerald-500/5 text-emerald-300",
                amber: "border-amber-500/30 bg-amber-500/5 text-amber-300",
                rose: "border-rose-500/30 bg-rose-500/5 text-rose-300",
                zinc: "border-zinc-500/30 bg-zinc-500/5 text-zinc-300",
              }[meta.color];
              return (
                <div
                  key={key}
                  data-testid={`parabolic-phase-${key}`}
                  className={`rounded-md border ${colorClasses} p-3`}
                >
                  <div className="flex items-center justify-between mb-1">
                    <Icon size={14} weight="duotone" />
                    <span className="text-[10px] uppercase tracking-widest opacity-70">
                      {meta.label}
                    </span>
                  </div>
                  <div className="text-2xl font-bold">{n}</div>
                  <div className="text-[10px] opacity-60 mt-1 truncate">
                    {(symbols[key] || []).slice(0, 3).map((s) => s.symbol).join(" · ") || "—"}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Surface the parabolic + topping watchlist explicitly */}
          {(counts.parabolic > 0 || counts.topping > 0) && (
            <div className="mt-2 text-[11px] space-y-1">
              {(symbols.parabolic || []).slice(0, 4).map((s) => (
                <div
                  key={`p-${s.symbol}`}
                  data-testid={`parabolic-watchlist-${s.symbol}`}
                  className="flex items-center justify-between text-amber-300/90 border border-amber-500/15 bg-amber-500/5 rounded px-2 py-1"
                >
                  <span className="font-mono">{s.symbol}</span>
                  <span className="opacity-70">
                    +{s.velocity_5m?.toFixed(1)}% / 5m · {s.rvol_acceleration?.toFixed(1)}× rvol-accel
                  </span>
                </div>
              ))}
              {(symbols.topping || []).slice(0, 4).map((s) => (
                <div
                  key={`t-${s.symbol}`}
                  data-testid={`topping-watchlist-${s.symbol}`}
                  className="flex items-center justify-between text-rose-300/90 border border-rose-500/20 bg-rose-500/5 rounded px-2 py-1"
                >
                  <span className="font-mono">{s.symbol}</span>
                  <span className="opacity-70">
                    -{s.peak_drop_pct?.toFixed(1)}% off peak — topping
                  </span>
                </div>
              ))}
            </div>
          )}

          <div className="text-[10px] opacity-40 mt-3 leading-relaxed">
            Adaptive sizing: parabolic +8% scales down to half-size; +20% to 0.
            Topping confirmed at 2 consecutive red bars after a green run.
            Thresholds env-tunable (PARABOLIC_5M_THRESHOLD_PCT, etc.).
          </div>
        </>
      )}
    </Card>
  );
}
