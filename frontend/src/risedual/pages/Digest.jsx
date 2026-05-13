import React, { useEffect, useState } from "react";
import { useTier } from "../context/TierContext";
import { mc, fmtAgo } from "../lib/mc";
import { Sparkles, TrendingUp, AlertTriangle, Eye } from "lucide-react";

function StatTile({ icon: Icon, label, value, hint, testid }) {
  return (
    <div
      data-testid={testid}
      className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-5"
    >
      <div className="mb-3 inline-flex h-8 w-8 items-center justify-center rounded-md bg-emerald-500/10 text-emerald-400">
        <Icon size={16} strokeWidth={1.8} />
      </div>
      <div className="text-[11px] font-mono uppercase tracking-[0.18em] text-zinc-500">
        {label}
      </div>
      <div className="mt-1 font-display text-2xl text-white">{value}</div>
      {hint && <div className="mt-1 text-[11px] text-zinc-500">{hint}</div>}
    </div>
  );
}

export default function Digest() {
  const { tier } = useTier();
  const [narrative, setNarrative] = useState({ loading: true, data: null, error: null });
  const [digest, setDigest] = useState({ loading: true, data: null, error: null });

  useEffect(() => {
    let cancelled = false;
    setNarrative({ loading: true, data: null, error: null });
    setDigest({ loading: true, data: null, error: null });
    mc.narrative(tier).then((r) => {
      if (cancelled) return;
      r.ok
        ? setNarrative({ loading: false, data: r.data, error: null })
        : setNarrative({ loading: false, data: null, error: r.detail });
    });
    mc.digest(tier).then((r) => {
      if (cancelled) return;
      r.ok
        ? setDigest({ loading: false, data: r.data, error: null })
        : setDigest({ loading: false, data: null, error: r.detail });
    });
    return () => {
      cancelled = true;
    };
  }, [tier]);

  const d = digest.data || {};
  const preds = d.predictions || [];
  const smartMoney = d.smart_money || [];
  const alerts = d.alerts || [];

  return (
    <div className="space-y-10" data-testid="rd-digest-page">
      <div>
        <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
          Today's Tape
        </div>
        <h1 className="mt-2 font-display text-3xl tracking-tight text-white md:text-4xl">
          The market, narrated.
        </h1>
      </div>

      {/* NARRATIVE */}
      <section
        data-testid="rd-narrative-section"
        className="relative overflow-hidden rounded-xl border border-zinc-900 bg-gradient-to-br from-zinc-950 to-black p-8"
      >
        <div className="absolute -right-10 -top-10 h-48 w-48 rounded-full bg-emerald-500/10 blur-3xl" />
        <div className="relative">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-[10px] font-mono uppercase tracking-[0.2em] text-emerald-300">
            <Sparkles size={11} strokeWidth={2} /> Narrative · Gemini 3
          </div>
          {narrative.loading && (
            <div data-testid="rd-narrative-loading" className="font-mono text-[12px] uppercase tracking-[0.18em] text-zinc-600">
              Composing today's overview…
            </div>
          )}
          {narrative.error && (
            <div data-testid="rd-narrative-error" className="text-[13px] text-rose-300">
              {narrative.error}
            </div>
          )}
          {narrative.data && (
            <>
              <p
                data-testid="rd-narrative-text"
                className="font-display text-lg leading-relaxed text-zinc-100 md:text-xl"
              >
                {narrative.data.text}
              </p>
              <div className="mt-4 flex items-center gap-3 font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-600">
                <span>{narrative.data.cached ? "cached" : "fresh"}</span>
                <span>·</span>
                <span>{fmtAgo(narrative.data.generated_at)}</span>
              </div>
            </>
          )}
        </div>
      </section>

      {/* STATS */}
      <section data-testid="rd-stats-section">
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <StatTile
            icon={Eye}
            label="Active Signals"
            value={digest.loading ? "…" : (d.active_signals ?? 0)}
            testid="rd-stat-active"
          />
          <StatTile
            icon={TrendingUp}
            label="Predictions"
            value={digest.loading ? "…" : preds.length}
            testid="rd-stat-predictions"
          />
          <StatTile
            icon={Sparkles}
            label="Smart Money"
            value={digest.loading ? "…" : smartMoney.length}
            testid="rd-stat-smart-money"
          />
          <StatTile
            icon={AlertTriangle}
            label="Alerts"
            value={digest.loading ? "…" : alerts.length}
            testid="rd-stat-alerts"
          />
        </div>
      </section>

      {/* PREDICTIONS */}
      <section data-testid="rd-predictions-section">
        <div className="mb-4 flex items-end justify-between">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
              AI Predictions
            </div>
            <h2 className="mt-1 font-display text-xl text-white">
              Where the council is leaning.
            </h2>
          </div>
        </div>
        {digest.loading ? (
          <div className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-8 text-center font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-600">
            Loading predictions…
          </div>
        ) : preds.length === 0 ? (
          <div className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-8 text-center text-[13px] text-zinc-500">
            No predictions on the board right now.
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-zinc-900">
            <table className="w-full text-left text-[13px]">
              <thead className="bg-zinc-950">
                <tr className="font-mono text-[10px] uppercase tracking-[0.16em] text-zinc-500">
                  <th className="px-4 py-3">Symbol</th>
                  <th className="px-4 py-3">Direction</th>
                  <th className="px-4 py-3">Confidence</th>
                  <th className="px-4 py-3 text-right">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-900">
                {preds.slice(0, 12).map((p, i) => {
                  const dir = (p.direction || p.stance || "HOLD").toUpperCase();
                  const toneCls =
                    dir === "LONG"
                      ? "text-emerald-300"
                      : dir === "SHORT"
                      ? "text-rose-300"
                      : "text-zinc-300";
                  const conf =
                    typeof p.confidence === "number"
                      ? Math.round(p.confidence > 1 ? p.confidence : p.confidence * 100)
                      : null;
                  return (
                    <tr
                      key={p.signal_id || p.symbol || i}
                      data-testid={`rd-prediction-row-${i}`}
                      className="hover:bg-zinc-950/70"
                    >
                      <td className="px-4 py-3 font-display text-white">
                        {p.symbol || "—"}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`font-mono text-[11px] uppercase tracking-[0.14em] ${toneCls}`}>
                          {dir}
                        </span>
                      </td>
                      <td className="px-4 py-3 font-mono text-zinc-300">
                        {conf !== null ? `${conf}%` : "—"}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-[11px] text-zinc-500">
                        {fmtAgo(p.updated_at || p.posted_at)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
