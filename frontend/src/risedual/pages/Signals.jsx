import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTier } from "../context/TierContext";
import { mc, fmtAgo } from "../lib/mc";
import { ShieldAlert, ArrowUpRight, ArrowDownRight, Minus } from "lucide-react";

function DirectionTag({ direction }) {
  const map = {
    LONG: {
      icon: ArrowUpRight,
      label: "LONG",
      cls: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
    },
    SHORT: {
      icon: ArrowDownRight,
      label: "SHORT",
      cls: "border-rose-500/30 bg-rose-500/10 text-rose-300",
    },
    HOLD: {
      icon: Minus,
      label: "HOLD",
      cls: "border-slate-500 bg-zinc-800/40 text-zinc-300",
    },
  };
  const m = map[direction] || map.HOLD;
  const Icon = m.icon;
  return (
    <span
      data-testid={`rd-signal-direction-${m.label.toLowerCase()}`}
      className={`inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 text-[11px] font-mono tracking-[0.12em] ${m.cls}`}
    >
      <Icon size={12} strokeWidth={2} /> {m.label}
    </span>
  );
}

function ConsensusBar({ buy, sell, hold }) {
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-slate-700/60">
      <div className="bg-emerald-500" style={{ width: `${buy}%` }} />
      <div className="bg-rose-500" style={{ width: `${sell}%` }} />
      <div className="bg-zinc-700" style={{ width: `${hold}%` }} />
    </div>
  );
}

function ConsensusHero({ consensus, count }) {
  const label = consensus?.label || "—";
  const toneCls =
    {
      BULLISH: "text-emerald-300",
      BEARISH: "text-rose-300",
      NEUTRAL: "text-zinc-300",
      MIXED: "text-amber-300",
    }[label] || "text-zinc-300";
  return (
    <div
      data-testid="rd-consensus-hero"
      className="rounded-xl border border-slate-700 bg-gradient-to-br from-slate-800 to-slate-900 p-6 md:p-8"
    >
      <div className="flex flex-col items-start justify-between gap-6 md:flex-row md:items-end">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
            AI Council Consensus
          </div>
          <div className={`mt-3 font-display text-4xl md:text-5xl ${toneCls}`}>
            {label}
          </div>
          <div className="mt-2 text-[12px] text-zinc-500">
            across {count} active signals
          </div>
        </div>
        <div className="w-full max-w-md">
          <ConsensusBar
            buy={consensus?.buy_pct || 0}
            sell={consensus?.sell_pct || 0}
            hold={consensus?.hold_pct || 0}
          />
          <div className="mt-2 flex justify-between font-mono text-[11px] uppercase tracking-[0.16em]">
            <span className="text-emerald-400">Buy {consensus?.buy_pct || 0}%</span>
            <span className="text-rose-400">Sell {consensus?.sell_pct || 0}%</span>
            <span className="text-zinc-500">Hold {consensus?.hold_pct || 0}%</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function SignalCard({ s }) {
  return (
    <Link
      to={`/signals/${s.signal_id}`}
      data-testid={`rd-signal-card-${s.signal_id}`}
      className="group block rounded-lg border border-slate-700 bg-slate-800/40 p-5 transition-colors hover:border-emerald-500/40"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <div className="font-display text-lg text-white">{s.symbol}</div>
            <DirectionTag direction={s.direction} />
            {s.flagged_by_auditor && (
              <span
                data-testid="rd-signal-auditor-flag"
                className="inline-flex items-center gap-1 rounded-sm border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-[0.14em] text-amber-300"
              >
                <ShieldAlert size={11} strokeWidth={2} /> Auditor flagged
              </span>
            )}
          </div>
          <div className="mt-1 font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
            {s.state} · {fmtAgo(s.updated_at)}
          </div>
        </div>
        <div className="text-right">
          <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
            Consensus
          </div>
          <div className="mt-1 font-display text-sm text-zinc-200">
            {s.consensus}
          </div>
        </div>
      </div>
      {s.thesis && (
        <p className="mt-3 line-clamp-2 text-[13px] leading-relaxed text-zinc-400">
          {s.thesis}
        </p>
      )}
      <div className="mt-4">
        <ConsensusBar
          buy={s.consensus_breakdown?.buy_pct || 0}
          sell={s.consensus_breakdown?.sell_pct || 0}
          hold={s.consensus_breakdown?.hold_pct || 0}
        />
        <div className="mt-1.5 flex justify-between font-mono text-[10px] uppercase tracking-[0.14em]">
          <span className="text-emerald-400">B {s.consensus_breakdown?.buy_pct || 0}</span>
          <span className="text-rose-400">S {s.consensus_breakdown?.sell_pct || 0}</span>
          <span className="text-zinc-500">H {s.consensus_breakdown?.hold_pct || 0}</span>
        </div>
      </div>
    </Link>
  );
}

export default function Signals() {
  const { tier } = useTier();
  const [state, setState] = useState({ loading: true, data: null, error: null });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, data: null, error: null });
    mc.signals(tier, 30).then((res) => {
      if (cancelled) return;
      if (res.ok) setState({ loading: false, data: res.data, error: null });
      else setState({ loading: false, data: null, error: res.detail });
    });
    return () => {
      cancelled = true;
    };
  }, [tier]);

  return (
    <div className="space-y-8" data-testid="rd-signals-page">
      <div>
        <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
          Active Signals
        </div>
        <h1 className="mt-2 font-display text-3xl tracking-tight text-white md:text-4xl">
          The tape, refereed.
        </h1>
        <p className="mt-3 max-w-xl text-[14px] text-zinc-400">
          Every open signal the AI council is currently watching. Direction is
          the Commander's call. Consensus is the cross-brain vote.
        </p>
      </div>

      {state.loading && (
        <div
          data-testid="rd-signals-loading"
          className="rounded-lg border border-slate-700 bg-slate-800/40 p-12 text-center font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-600"
        >
          Pulling council vote…
        </div>
      )}

      {state.error && (
        <div
          data-testid="rd-signals-error"
          className="rounded-lg border border-rose-900/40 bg-rose-950/30 p-6 text-[13px] text-rose-300"
        >
          {state.error}
        </div>
      )}

      {state.data && (
        <>
          <ConsensusHero
            consensus={state.data.consensus}
            count={state.data.active_signals || 0}
          />
          {(state.data.items || []).length === 0 ? (
            <div
              data-testid="rd-signals-empty"
              className="rounded-lg border border-slate-700 bg-slate-800/40 p-12 text-center text-[13px] text-zinc-500"
            >
              No active signals right now. The council is watching.
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3" data-testid="rd-signals-grid">
              {(state.data.items || []).map((s) => (
                <SignalCard key={s.signal_id} s={s} />
              ))}
            </div>
          )}

          {/* Smart-money expanded context — congressional, insider,
              institutional filings. Brains read from the same cache. */}
          <div className="mt-8">
            <div className="mb-3 text-[10px] font-mono uppercase tracking-[0.22em] text-slate-500">
              Smart Money — Background Context
            </div>
            <DarkPoolWidget expanded={true} />
          </div>
        </>
      )}
    </div>
  );
}
