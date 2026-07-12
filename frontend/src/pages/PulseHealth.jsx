import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";

/**
 * PulseHealth — the P5 tile.
 *
 * Reads `mc_pulse_health_snapshots` per brain via
 * `GET /api/mc/pulse-health/{brain}` (live) +
 * `GET /api/mc/pulse-health/{brain}/history` (trend).
 *
 * Purpose: prove the four brains stay four minds. Surfaces
 *   • distinctness (target > 0.15)
 *   • action distribution (LONG/SHORT/FLAT %)
 *   • confidence mean + std (multiplier is doing SOMETHING)
 *   • input health: stale/no_data/exception rates
 *   • dup opinion rate + pulse lag
 *
 * Warnings fire on:
 *   • distinctness < 0.10 → brains have merged
 *   • conf_std ≈ 0 → confidence saturated
 *   • no_data_rate > 0.80 → brain is silent most of the time
 *   • exception_rate > 0.05 → containment catching real bugs
 *   • duplicate_opinion_rate > 0.90 → cadence not binding
 *   • stale_input_rate > 0.20 → feeders starving the brain
 */

const BRAINS = ["camino", "gto", "barracuda", "hellcat"];

const BRAIN_META = {
  camino:    { display: "Camino",    strategy: "Trend Following",       family: "TREND"    },
  gto:       { display: "GTO",       strategy: "Momentum Confirmation", family: "MOMENTUM" },
  barracuda: { display: "Barracuda", strategy: "Mean Reversion",        family: "MEAN"     },
  hellcat:   { display: "Hellcat",   strategy: "Execution Safety",      family: "EXEC"     },
};

const THRESHOLDS = {
  distinctness_floor: 0.10,       // < → brains merged
  distinctness_target: 0.20,      // >= → healthy
  conf_std_floor: 0.02,           // <= → saturated
  no_data_ceiling: 0.80,          // > → silent
  exception_ceiling: 0.05,        // > → bugs
  duplicate_ceiling: 0.90,        // > → cadence broken
  stale_ceiling: 0.20,            // > → starving
};

function pct(n) {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function fmt(n, digits = 3) {
  if (n === null || n === undefined) return "—";
  return typeof n === "number" ? n.toFixed(digits) : String(n);
}

function warningsFor(doc) {
  if (!doc) return [];
  const w = [];
  const dist = doc?.distinctness?.distinctness;
  if (dist !== null && dist !== undefined && dist < THRESHOLDS.distinctness_floor) {
    w.push({ level: "high", msg: `distinctness ${fmt(dist)} < ${THRESHOLDS.distinctness_floor} — brain has merged with peers` });
  }
  if (doc.confidence_std !== null && doc.confidence_std <= THRESHOLDS.conf_std_floor) {
    w.push({ level: "high", msg: `confidence_std ${fmt(doc.confidence_std)} — saturated` });
  }
  if (doc.no_data_rate > THRESHOLDS.no_data_ceiling) {
    w.push({ level: "med", msg: `silent on ${pct(doc.no_data_rate)} of pulses` });
  }
  if (doc.exception_rate > THRESHOLDS.exception_ceiling) {
    w.push({ level: "high", msg: `containment caught exceptions on ${pct(doc.exception_rate)} of pulses` });
  }
  if (doc.duplicate_opinion_rate > THRESHOLDS.duplicate_ceiling) {
    w.push({ level: "med", msg: `duplicate opinions ${pct(doc.duplicate_opinion_rate)} — cadence cool-down not binding` });
  }
  if (doc.stale_input_rate > THRESHOLDS.stale_ceiling) {
    w.push({ level: "med", msg: `stale inputs ${pct(doc.stale_input_rate)} — feeders starving the brain` });
  }
  return w;
}

function distinctnessTone(v) {
  if (v === null || v === undefined) return "muted";
  if (v < THRESHOLDS.distinctness_floor) return "red";
  if (v >= THRESHOLDS.distinctness_target) return "green";
  return "yellow";
}

function BrainTile({ brain, live, history }) {
  const meta = BRAIN_META[brain];
  const dist = live?.distinctness?.distinctness;
  const tone = distinctnessTone(dist);
  const actions = live?.action_distribution?.pct || {};
  const warnings = warningsFor(live);

  // Sparkline: last 24 snapshot distinctness values.
  const spark = (history?.snapshots || [])
    .slice(0, 24).reverse()
    .map(s => s?.distinctness?.distinctness ?? null);

  return (
    <Card data-testid={`pulse-health-tile-${brain}`}>
      <div className="flex items-baseline justify-between">
        <div>
          <div className="text-lg font-semibold">{meta.display}</div>
          <div className="text-xs opacity-60">
            {meta.strategy} · reason family: <span className="font-mono">{meta.family}_*</span>
            <RegimePill regime={live?.market_regime} />
          </div>
        </div>
        <div className={`text-3xl font-mono tabular-nums ${
          tone === "red"    ? "text-red-500" :
          tone === "green"  ? "text-emerald-500" :
          tone === "yellow" ? "text-amber-500" : "opacity-40"
        }`}
          data-testid={`pulse-health-distinctness-${brain}`}
        >
          {dist === null || dist === undefined ? "—" : dist.toFixed(3)}
        </div>
      </div>
      <div className="text-xs opacity-60 -mt-1 mb-2">distinctness (target ≥ {THRESHOLDS.distinctness_target})</div>

      <Sparkline values={spark} tone={tone} />

      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <div>
          <div className="opacity-60">LONG</div>
          <div className="font-mono tabular-nums" data-testid={`pulse-health-long-${brain}`}>{pct((actions.LONG || 0) / 100)}</div>
        </div>
        <div>
          <div className="opacity-60">SHORT</div>
          <div className="font-mono tabular-nums" data-testid={`pulse-health-short-${brain}`}>{pct((actions.SHORT || 0) / 100)}</div>
        </div>
        <div>
          <div className="opacity-60">FLAT</div>
          <div className="font-mono tabular-nums" data-testid={`pulse-health-flat-${brain}`}>{pct((actions.FLAT || 0) / 100)}</div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
        <Stat label="evals"    value={live?.evaluation_count} testid={`pulse-health-evals-${brain}`} />
        <Stat label="conf_mean" value={fmt(live?.confidence_mean, 3)} />
        <Stat label="conf_std"  value={fmt(live?.confidence_std, 3)} />
        <Stat label="lag_ms"    value={live?.pulse_lag_ms === null ? "—" : Math.round(live?.pulse_lag_ms)} />
        <Stat label="stale_in"   value={pct(live?.stale_input_rate)} />
        <Stat label="no_data"    value={pct(live?.no_data_rate)} testid={`pulse-health-no-data-${brain}`} />
        <Stat label="exceptions" value={pct(live?.exception_rate)} />
        <Stat label="duplicates" value={pct(live?.duplicate_opinion_rate)} />
      </div>

      <NoDataBreakdown brain={brain} breakdown={live?.no_data_breakdown} noDataRate={live?.no_data_rate} />

      <ArbiterAlignment brain={brain} alignment={live?.arbiter_alignment} />

      <DissentCorrectness brain={brain} dissent={live?.dissent_correctness} />

      {warnings.length > 0 && (
        <div className="mt-3 space-y-1" data-testid={`pulse-health-warnings-${brain}`}>
          {warnings.map((w, i) => (
            <div key={i} className={`text-xs px-2 py-1 rounded ${
              w.level === "high" ? "bg-red-500/15 text-red-500" :
              "bg-amber-500/15 text-amber-500"
            }`}>{w.msg}</div>
          ))}
        </div>
      )}
    </Card>
  );
}

function Stat({ label, value, testid }) {
  return (
    <div className="flex items-baseline justify-between">
      <span className="opacity-60">{label}</span>
      <span className="font-mono tabular-nums" data-testid={testid}>{value ?? "—"}</span>
    </div>
  );
}

// P1 (2026-02-11): reason-code breakdown for `no_data_rate`. The
// aggregate scalar tells the operator that the brain is silent —
// this breaks it down into WHY. Reason vocabulary matches
// `mc_pulse.receipt.BrainSilence`:
//   snapshot_stale   → feed was too old / market closed
//   cadence_cooldown → brain intentionally skipped (already saw this bar)
//   no_signal_return → brain evaluated but returned None (rare bug signal)
//   unknown          → pre-P1 pulse (before we stamped BrainSilence rows)
const NO_DATA_REASON_LABEL = {
  snapshot_stale:   "market closed / stale feed",
  cadence_cooldown: "cadence cooldown",
  no_signal_return: "brain returned no signal",
  unknown:          "unclassified (pre-P1 pulse)",
};

function NoDataBreakdown({ brain, breakdown, noDataRate }) {
  const entries = Object.entries(breakdown || {})
    .filter(([, v]) => (v?.count || 0) > 0)
    .sort(([, a], [, b]) => (b.percent || 0) - (a.percent || 0));
  if (entries.length === 0) return null;
  return (
    <div
      className="mt-2 pl-2 border-l border-white/10 text-xs space-y-0.5"
      data-testid={`pulse-health-no-data-breakdown-${brain}`}
    >
      <div className="opacity-40 text-[10px] uppercase tracking-wider mb-0.5">
        why silent
      </div>
      {entries.map(([reason, v]) => (
        <div
          key={reason}
          className="flex items-baseline justify-between opacity-70"
          data-testid={`pulse-health-no-data-reason-${brain}-${reason}`}
        >
          <span className="opacity-70">
            {NO_DATA_REASON_LABEL[reason] || reason}
          </span>
          <span className="font-mono tabular-nums">
            {v.percent?.toFixed(1)}%
          </span>
        </div>
      ))}
    </div>
  );
}

// P4 (2026-02-11): Brain Influence — how often the arbiter picked
// this brain's opinion when the brain participated in the decision.
// A brain with high alignment is *materially influencing* the
// council. A brain with high participation but ~zero alignment is
// contributing diverse readings that the arbiter systematically
// discounts. Read the docstring on `_arbiter_alignment` for the
// full interpretation guide.
function ArbiterAlignment({ brain, alignment }) {
  const rate = alignment?.alignment_rate;
  const participated = alignment?.participated ?? 0;
  const wins = alignment?.wins ?? 0;
  // Hide entirely if the brain hasn't participated in any decision
  // in the window — no signal to report.
  if (participated === 0) return null;
  return (
    <div
      className="mt-2 pt-2 border-t border-white/5 flex items-baseline justify-between text-xs"
      data-testid={`pulse-health-arbiter-alignment-${brain}`}
    >
      <div>
        <span className="opacity-60">arbiter alignment</span>
        <span className="opacity-40 ml-2 text-[10px]">
          ({wins}/{participated})
        </span>
      </div>
      <span
        className="font-mono tabular-nums"
        data-testid={`pulse-health-arbiter-alignment-rate-${brain}`}
      >
        {rate === null || rate === undefined ? "—" : `${(rate * 100).toFixed(1)}%`}
      </span>
    </div>
  );
}

// P2 (2026-02-11): tiny inline pill in the tile header showing the
// current market regime the health snapshot was taken in. Colour-
// tinted so the operator picks up regime context at a glance and
// can interpret distinctness/alignment appropriately (a brain that
// looks "boring" in a strong bull may light up in choppy).
const REGIME_TONE = {
  bull:    "text-emerald-400 border-emerald-400/30 bg-emerald-400/10",
  bear:    "text-red-400     border-red-400/30     bg-red-400/10",
  choppy:  "text-amber-400   border-amber-400/30   bg-amber-400/10",
  unknown: "text-white/40    border-white/10       bg-white/5",
};

function RegimePill({ regime }) {
  const key = regime || "unknown";
  const tone = REGIME_TONE[key] || REGIME_TONE.unknown;
  return (
    <span
      className={`ml-2 inline-block px-1.5 py-0.5 text-[10px] rounded border font-mono ${tone}`}
      data-testid="pulse-health-regime-pill"
    >
      {key}
    </span>
  );
}

// P3 (2026-02-11): dissent correctness — when this brain disagrees
// with peer consensus and the outcome is later resolved, how often
// was the brain right? Sample-gated: renders "gathering samples
// (N / 50)" until enough resolved dissents exist. Once N ≥ 50 the
// rate takes over. Never hides — the placeholder itself is
// operator-actionable (tells you the metric is being farmed).
function DissentCorrectness({ brain, dissent }) {
  if (!dissent) return null;
  const resolved = dissent.resolved ?? 0;
  const correct = dissent.correct ?? 0;
  const rate = dissent.correctness_rate;
  const gathering = dissent.gathering_samples;
  const minSamples = dissent.min_samples ?? 50;
  return (
    <div
      className="mt-2 pt-2 border-t border-white/5 flex items-baseline justify-between text-xs"
      data-testid={`pulse-health-dissent-correctness-${brain}`}
    >
      <div>
        <span className="opacity-60">dissent correctness</span>
        {resolved > 0 && (
          <span className="opacity-40 ml-2 text-[10px]">
            ({correct}/{resolved})
          </span>
        )}
      </div>
      <span
        className="font-mono tabular-nums"
        data-testid={`pulse-health-dissent-rate-${brain}`}
      >
        {gathering
          ? <span className="opacity-40 text-[11px]">gathering ({resolved}/{minSamples})</span>
          : rate === null || rate === undefined
            ? "—"
            : `${(rate * 100).toFixed(1)}%`}
      </span>
    </div>
  );
}

function Sparkline({ values, tone }) {
  const n = values?.length || 0;
  if (n < 2) return <div className="h-8 opacity-30 text-xs">not enough snapshots yet</div>;
  const nonNull = values.filter(v => v !== null && v !== undefined);
  if (nonNull.length < 2) return <div className="h-8 opacity-30 text-xs">not enough snapshots yet</div>;
  const min = Math.min(...nonNull);
  const max = Math.max(...nonNull);
  const range = Math.max(0.02, max - min);
  const w = 240, h = 32;
  const step = n > 1 ? w / (n - 1) : 0;
  const stroke =
    tone === "red"    ? "#ef4444" :
    tone === "green"  ? "#10b981" :
    tone === "yellow" ? "#f59e0b" : "#6b7280";
  const points = values.map((v, i) => {
    if (v === null || v === undefined) return null;
    const x = i * step;
    const y = h - ((v - min) / range) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).filter(Boolean).join(" ");
  return (
    <svg width={w} height={h} className="opacity-90">
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth="1.5"
        points={points}
      />
    </svg>
  );
}

export default function PulseHealth() {
  const [live, setLive] = useState({});
  const [history, setHistory] = useState({});
  const [err, setErr] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const fetchAll = async () => {
      try {
        const results = await Promise.all(
          BRAINS.flatMap(b => [
            api.get(`/mc/pulse-health/${b}?hours=1`)
              .catch(e => ({ data: { _error: e?.response?.data?.detail || e.message } })),
            api.get(`/mc/pulse-health/${b}/history?limit=48`)
              .catch(e => ({ data: { _error: e?.response?.data?.detail || e.message } })),
          ])
        );
        if (cancelled) return;
        const liveMap = {};
        const histMap = {};
        BRAINS.forEach((b, i) => {
          liveMap[b] = results[i * 2].data;
          histMap[b] = results[i * 2 + 1].data;
        });
        setLive(liveMap);
        setHistory(histMap);
        setLoaded(true);
      } catch (e) {
        if (!cancelled) setErr(e?.response?.data?.detail || e.message);
      }
    };
    fetchAll();
    const timer = setInterval(fetchAll, 30000);  // refresh every 30s
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  if (err) {
    return (
      <div className="p-6">
        <PageHeader title="Pulse Health" />
        <div className="text-red-500">error: {err}</div>
      </div>
    );
  }
  if (!loaded) {
    return (
      <div className="p-6">
        <PageHeader title="Pulse Health" />
        <LoadingRow />
      </div>
    );
  }

  // Cross-brain "identical action distribution" warning — the operator
  // called this out explicitly. If 3+ brains have IDENTICAL action
  // percentages, the strategies have collapsed.
  const actionSigs = BRAINS.map(b => {
    const p = live[b]?.action_distribution?.pct || {};
    return `${(p.LONG || 0).toFixed(2)}/${(p.SHORT || 0).toFixed(2)}/${(p.FLAT || 0).toFixed(2)}`;
  });
  const dupSigs = actionSigs.filter((sig, i, arr) => arr.indexOf(sig) !== i);
  const dupCount = new Set(dupSigs).size;
  const collapseAlert = dupCount > 0;

  return (
    <div className="p-6 space-y-4" data-testid="pulse-health-page">
      <PageHeader
        title="Pulse Health"
        subtitle="Four brains, four minds. Distinctness ≥ 0.20 = healthy separation."
      />

      {collapseAlert && (
        <div className="rounded bg-amber-500/15 text-amber-500 p-3 text-sm" data-testid="pulse-health-collapse-alert">
          <span className="font-semibold">⚠ action distribution collapse:</span>{" "}
          {actionSigs.map((s, i) => (
            <span key={i} className="mr-3 font-mono">
              {BRAIN_META[BRAINS[i]].display}={s}
            </span>
          ))}
          <div className="text-xs opacity-80 mt-1">
            Two or more brains produced identical LONG/SHORT/FLAT percentages in the last hour.
            Genuine agreement on strong evidence is fine; sustained identical distributions across
            all conditions is not.
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4" data-testid="pulse-health-grid">
        {BRAINS.map(b => (
          <BrainTile key={b} brain={b} live={live[b]} history={history[b]} />
        ))}
      </div>

      <div className="text-xs opacity-60 pt-2">
        <div>Auto-refresh: every 30s. Snapshots persist every 15 min (24h TTL 30d).</div>
        <div>Doctrine: <code>mc_pulse_health_snapshots</code> is the durable schema (P4 rename retired runner-relative parity metrics).</div>
        <div>API: <code>GET /api/mc/pulse-health/{"{brain}"}</code> live · <code>/history?limit=N</code> trend.</div>
      </div>
    </div>
  );
}
