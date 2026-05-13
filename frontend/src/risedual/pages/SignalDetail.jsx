import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useTier } from "../context/TierContext";
import { mc, fmtAgo } from "../lib/mc";
import CandleChart from "../components/CandleChart";
import {
  ArrowLeft, ArrowUpRight, ArrowDownRight, Minus,
  ShieldAlert, Shield, Crosshair, Sword,
} from "lucide-react";

const DIR_CLS = {
  LONG: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  SHORT: "border-rose-500/30 bg-rose-500/10 text-rose-300",
  HOLD: "border-slate-500 bg-zinc-800/40 text-zinc-300",
};
const DIR_ICON = { LONG: ArrowUpRight, SHORT: ArrowDownRight, HOLD: Minus };

function DirectionBadge({ dir }) {
  const Icon = DIR_ICON[dir] || Minus;
  const cls = DIR_CLS[dir] || DIR_CLS.HOLD;
  return (
    <span className={`inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 font-mono text-[11px] tracking-[0.12em] ${cls}`}>
      <Icon size={12} strokeWidth={2} /> {dir}
    </span>
  );
}

function StanceTag({ stance }) {
  const s = (stance || "").toUpperCase();
  const tone =
    s === "LONG" ? "text-emerald-300"
    : s === "SHORT" ? "text-rose-300"
    : s === "VETO" ? "text-amber-300"
    : "text-zinc-300";
  return <span className={`font-mono text-[11px] uppercase tracking-[0.14em] ${tone}`}>{s}</span>;
}

function SideCard({ side, icon: Icon, accentCls, title, data, testid }) {
  return (
    <div
      data-testid={testid}
      className={`relative overflow-hidden rounded-lg border p-6 ${accentCls}`}
    >
      <div className="absolute -right-6 -top-6 opacity-10">
        <Icon size={88} strokeWidth={1} />
      </div>
      <div className="relative">
        <div className="mb-3 flex items-center gap-2">
          <Icon size={14} strokeWidth={1.8} />
          <span className="font-mono text-[10px] uppercase tracking-[0.22em]">{title}</span>
        </div>
        {data ? (
          <>
            <div className="flex items-center gap-2">
              <StanceTag stance={data.stance} />
              {typeof data.confidence === "number" && (
                <span className="font-mono text-[11px] text-zinc-400">
                  · conf {data.confidence}%
                </span>
              )}
            </div>
            <p className="mt-3 line-clamp-6 text-[13px] leading-relaxed text-zinc-200/90">
              {data.notes || <span className="text-zinc-500 italic">No notes attached.</span>}
            </p>
          </>
        ) : (
          <div className="text-[12px] italic text-zinc-500">
            Seat unfilled — no stance from {title.toLowerCase()} yet.
          </div>
        )}
      </div>
    </div>
  );
}

function ConsensusBar({ buy, sell, hold }) {
  return (
    <div className="flex h-2 w-full overflow-hidden rounded-full bg-slate-700/60">
      <div className="bg-emerald-500" style={{ width: `${buy}%` }} />
      <div className="bg-rose-500" style={{ width: `${sell}%` }} />
      <div className="bg-zinc-700" style={{ width: `${hold}%` }} />
    </div>
  );
}

export default function SignalDetail() {
  const { id } = useParams();
  const { tier } = useTier();
  const [state, setState] = useState({ loading: true, data: null, error: null });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, data: null, error: null });
    mc.signal(tier, id).then((r) => {
      if (cancelled) return;
      r.ok
        ? setState({ loading: false, data: r.data, error: null })
        : setState({ loading: false, data: null, error: r.detail });
    });
    return () => { cancelled = true; };
  }, [tier, id]);

  const d = state.data || {};
  const adv = d.adversarial || {};
  const gov = d.governance || {};
  const counts = d.consensus_breakdown || {};
  const auditorVeto = gov.auditor?.action === "VETO";

  return (
    <div className="space-y-10" data-testid="rd-signal-detail-page">
      <div>
        <Link
          to="/r/signals"
          data-testid="rd-signal-back"
          className="inline-flex items-center gap-1 font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500 transition-colors hover:text-zinc-200"
        >
          <ArrowLeft size={12} strokeWidth={2} /> All signals
        </Link>
      </div>

      {state.loading && (
        <div data-testid="rd-signal-loading" className="rounded-lg border border-slate-700 bg-slate-800/40 p-12 text-center font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-600">
          Pulling the council's vote…
        </div>
      )}

      {state.error && (
        <div data-testid="rd-signal-error" className="rounded-lg border border-rose-900/40 bg-rose-950/30 p-6 text-[13px] text-rose-300">
          {state.error}
        </div>
      )}

      {state.data && (
        <>
          {/* HEADER */}
          <section className="rounded-xl border border-slate-700 bg-gradient-to-br from-slate-800 to-slate-900 p-6 md:p-8" data-testid="rd-signal-header">
            <div className="flex flex-col items-start justify-between gap-6 md:flex-row md:items-end">
              <div>
                <div className="flex items-center gap-3">
                  <h1 className="font-display text-4xl tracking-tight text-white md:text-5xl">
                    {d.symbol}
                  </h1>
                  <DirectionBadge dir={d.direction} />
                  {d.flagged_by_auditor && (
                    <span
                      data-testid="rd-signal-auditor-flag"
                      className="inline-flex items-center gap-1 rounded-sm border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] text-amber-300"
                    >
                      <ShieldAlert size={11} strokeWidth={2} /> Auditor flagged
                    </span>
                  )}
                </div>
                <div className="mt-2 font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
                  {d.state} · updated {fmtAgo(d.updated_at)} · opened {fmtAgo(d.created_at)}
                </div>
                {d.thesis && (
                  <p className="mt-4 max-w-2xl text-[14px] leading-relaxed text-zinc-300">
                    {d.thesis}
                  </p>
                )}
              </div>
              <div className="w-full max-w-sm space-y-2">
                <div className="flex items-end justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">Consensus</span>
                  <span className="font-display text-base text-zinc-200">{d.consensus}</span>
                </div>
                <ConsensusBar
                  buy={counts.buy_pct || 0}
                  sell={counts.sell_pct || 0}
                  hold={counts.hold_pct || 0}
                />
                <div className="flex justify-between font-mono text-[10px] uppercase tracking-[0.16em]">
                  <span className="text-emerald-400">Buy {counts.buy_pct || 0}%</span>
                  <span className="text-rose-400">Sell {counts.sell_pct || 0}%</span>
                  <span className="text-zinc-500">Hold {counts.hold_pct || 0}%</span>
                </div>
              </div>
            </div>
          </section>

          {/* CANDLE CHART */}
          <section data-testid="rd-chart-section">
            <div className="mb-4">
              <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-slate-500">Price action</div>
              <h2 className="mt-1 font-display text-xl text-white">Candles · live tape.</h2>
            </div>
            <CandleChart symbol={d.symbol} />
          </section>

          {/* ADVERSARIAL — Bull / Bear / Commander */}
          <section data-testid="rd-adversarial-section">
            <div className="mb-4">
              <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">War Room</div>
              <h2 className="mt-1 font-display text-xl text-white">Bull case · Bear case · Commander.</h2>
            </div>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
              <SideCard
                side="bull"
                icon={Shield}
                title="Bull Case"
                accentCls="border-emerald-500/30 bg-emerald-500/5 text-emerald-100"
                data={adv.bull}
                testid="rd-adv-bull"
              />
              <SideCard
                side="bear"
                icon={Sword}
                title="Bear Case"
                accentCls="border-rose-500/30 bg-rose-500/5 text-rose-100"
                data={adv.bear}
                testid="rd-adv-bear"
              />
              <SideCard
                side="commander"
                icon={Crosshair}
                title="Commander"
                accentCls="border-slate-600 bg-slate-800/60 text-zinc-100"
                data={adv.commander}
                testid="rd-adv-commander"
              />
            </div>
          </section>

          {/* GOVERNANCE — Strategist / Auditor / Synthesized */}
          <section data-testid="rd-governance-section">
            <div className="mb-4">
              <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">Pipeline</div>
              <h2 className="mt-1 font-display text-xl text-white">Strategist → Auditor → Synthesized.</h2>
            </div>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
              <div className="rounded-lg border border-slate-700 bg-slate-800/40 p-5" data-testid="rd-gov-strategist">
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">
                  {gov.strategist?.label || "STRATEGIST_AGENT"}
                </div>
                <div className="mt-2 font-display text-base text-white">
                  {gov.strategist?.proposal || "—"}
                </div>
                <div className="mt-1 font-mono text-[11px] text-zinc-400">
                  Confidence {gov.strategist?.confidence ?? 0}%
                </div>
                <div className="mt-3 text-[12px] text-zinc-400">
                  {gov.strategist?.detected || "AWAITING_PROPOSAL"}
                </div>
              </div>

              <div
                className={`rounded-lg border p-5 ${auditorVeto ? "border-amber-500/40 bg-amber-500/5" : "border-slate-700 bg-slate-800/40"}`}
                data-testid="rd-gov-auditor"
              >
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">
                  {gov.auditor?.label || "RISK_AUDITOR_AGENT"}
                </div>
                <div className={`mt-2 font-display text-base ${auditorVeto ? "text-amber-300" : "text-white"}`}>
                  {gov.auditor?.action || "PASS"}
                </div>
                <div className="mt-1 font-mono text-[11px] text-zinc-400">
                  {gov.auditor?.mode || "—"}
                </div>
                {typeof gov.auditor?.confidence === "number" && gov.auditor.confidence > 0 && (
                  <div className="mt-3 text-[12px] text-zinc-400">
                    Confidence {gov.auditor.confidence}%
                  </div>
                )}
              </div>

              <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-5" data-testid="rd-gov-synth">
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-emerald-300/70">
                  {gov.synthesized?.label || "SYNTHESIZED SIGNAL"}
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <span className="font-display text-base text-white">
                    {gov.synthesized?.symbol || d.symbol}
                  </span>
                  <DirectionBadge dir={gov.synthesized?.direction || "HOLD"} />
                </div>
                <div className="mt-2 font-mono text-[11px] text-zinc-300">
                  Confidence {gov.synthesized?.confidence ?? 0}%
                </div>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
