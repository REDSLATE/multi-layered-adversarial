import React, { useEffect, useRef, useState } from "react";
import { createChart, CandlestickSeries, HistogramSeries, ColorType } from "lightweight-charts";
import { useTier } from "../context/TierContext";

const TFS = [
  { id: "1m", label: "1m" },
  { id: "5m", label: "5m" },
  { id: "15m", label: "15m" },
  { id: "1h", label: "1h" },
  { id: "4h", label: "4h" },
  { id: "1d", label: "1d" },
];

const CANDLE_COLORS = {
  upColor: "#10B981",
  downColor: "#F43F5E",
  borderUpColor: "#10B981",
  borderDownColor: "#F43F5E",
  wickUpColor: "#10B981",
  wickDownColor: "#F43F5E",
};

function _toChartTime(iso) {
  return Math.floor(new Date(iso).getTime() / 1000);
}

export default function CandleChart({ symbol }) {
  const { tier } = useTier();
  const containerRef = useRef(null);
  const [tf, setTf] = useState("1h");
  const [meta, setMeta] = useState({ loading: true, error: null, count: 0, source: null });

  // Effect: create chart + fetch + render in one effect, re-run when symbol/tf change.
  useEffect(() => {
    if (!containerRef.current || !symbol) return;
    let cancelled = false;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 380,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#94A3B8",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 11,
      },
      // Pin locale so weird env locales (e.g. en-US@posix in headless browsers)
      // don't crash Date.toLocaleString inside lightweight-charts.
      localization: {
        locale: "en-US",
        dateFormat: "yyyy-MM-dd",
      },
      grid: {
        vertLines: { color: "rgba(51,65,85,0.25)" },
        horzLines: { color: "rgba(51,65,85,0.25)" },
      },
      rightPriceScale: { borderColor: "#334155" },
      timeScale: { borderColor: "#334155", timeVisible: true, secondsVisible: false },
    });
    const candleSeries = chart.addSeries(CandlestickSeries, CANDLE_COLORS);
    const volSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

    const resize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", resize);

    setMeta({ loading: true, error: null, count: 0, source: null });

    fetch(
      `${process.env.REACT_APP_BACKEND_URL}/api/public/bars/${symbol}?tf=${tf}&limit=300`,
      {
        headers: {
          "X-RiseDual-Token": process.env.REACT_APP_RISEDUAL_TOKEN || "",
          "X-RiseDual-User-Tier": tier || "free",
        },
      },
    )
      .then(async (r) => {
        const data = await r.json().catch(() => null);
        if (cancelled) return;
        if (!r.ok) {
          setMeta({ loading: false, error: data?.detail || `HTTP ${r.status}`, count: 0, source: null });
          return;
        }
        const seen = new Set();
        const candles = [];
        const volumes = [];
        for (const b of (data.bars || [])) {
          const t = _toChartTime(b.ts);
          if (seen.has(t)) continue;
          seen.add(t);
          candles.push({ time: t, open: +b.o, high: +b.h, low: +b.l, close: +b.c });
          volumes.push({
            time: t,
            value: +b.v || 0,
            color: +b.c >= +b.o ? "rgba(16,185,129,0.35)" : "rgba(244,63,94,0.35)",
          });
        }
        candles.sort((a, b) => a.time - b.time);
        volumes.sort((a, b) => a.time - b.time);
        try {
          candleSeries.setData(candles);
          volSeries.setData(volumes);
          chart.timeScale().fitContent();
        } catch (e) {
          console.error("[CandleChart] setData failed:", e);
          setMeta({ loading: false, error: String(e?.message || e), count: 0, source: null });
          return;
        }
        setMeta({
          loading: false,
          error: null,
          count: candles.length,
          source: data.source,
        });
      })
      .catch((e) => {
        if (cancelled) return;
        setMeta({ loading: false, error: e.message || "Network error", count: 0, source: null });
      });

    return () => {
      cancelled = true;
      window.removeEventListener("resize", resize);
      chart.remove();
    };
  }, [symbol, tf, tier]);

  return (
    <div
      data-testid="rd-candle-chart"
      className="rounded-xl border border-slate-700 bg-slate-800/40 p-4"
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <div className="font-display text-sm text-white">{symbol}</div>
          {meta.source && (
            <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">
              · {meta.source}
            </span>
          )}
        </div>
        <div className="flex rounded-md border border-slate-700 bg-slate-900 p-0.5" data-testid="rd-candle-tf">
          {TFS.map((t) => {
            const active = t.id === tf;
            return (
              <button
                key={t.id}
                onClick={() => setTf(t.id)}
                data-testid={`rd-candle-tf-${t.id}`}
                className={
                  "px-2 py-1 text-[10px] font-mono uppercase tracking-[0.14em] rounded transition-colors " +
                  (active
                    ? "bg-emerald-500/20 text-emerald-300"
                    : "text-slate-500 hover:text-slate-200")
                }
              >
                {t.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="relative">
        <div ref={containerRef} className="h-[380px] w-full" data-testid="rd-candle-canvas" />
        {meta.loading && (
          <div className="absolute inset-0 flex items-center justify-center font-mono text-[10px] uppercase tracking-[0.18em] text-slate-600">
            Loading bars…
          </div>
        )}
        {meta.error && !meta.loading && (
          <div data-testid="rd-candle-error" className="absolute inset-0 flex items-center justify-center px-4 text-center text-[12px] text-rose-300">
            {meta.error}
          </div>
        )}
        {!meta.loading && !meta.error && meta.count === 0 && (
          <div data-testid="rd-candle-empty" className="absolute inset-0 flex items-center justify-center text-center text-[12px] text-slate-500">
            No bars on file for {symbol} · {tf}
          </div>
        )}
      </div>

      {!meta.loading && !meta.error && meta.count > 0 && (
        <div className="mt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-slate-600">
          {meta.count} bars · {tf}
        </div>
      )}
    </div>
  );
}
