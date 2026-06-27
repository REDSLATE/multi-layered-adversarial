/**
 * TradeFlow — single-purpose "is this lane trading? if not, why?" page.
 *
 * Doctrine pin (2026-06-26, operator-driven):
 *     Operator said "there's too many things happening on the page" —
 *     Diagnostics is a tile-collage. This page is the opposite. ONE
 *     answer per lane at the top in a single sentence. Everything else
 *     collapsed behind a tap.
 *
 *     Designed for mobile. Designed to be opened, glanced at, closed.
 *
 * Layout (per lane card):
 *     ┌─ EQUITY ─────────────────────────────────┐
 *     │  ● BLOCKED BY: low_confidence           │
 *     │                                          │
 *     │  1,178 intents passed dry-run then died │
 *     │  at `low_confidence`. …                  │
 *     │                                          │
 *     │  [ FIX ] [ details ▾ ]                   │
 *     └──────────────────────────────────────────┘
 */
import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/ui-bits";

const POLL_MS = 30_000;
const LANES = ["equity", "crypto"];
const STATUS_COLORS = {
  TRADING: { bg: "bg-emerald-950/40", border: "border-emerald-700/50", text: "text-emerald-300", dot: "bg-emerald-400" },
  BLOCKED: { bg: "bg-rose-950/40", border: "border-rose-700/50", text: "text-rose-300", dot: "bg-rose-500" },
  UNCLEAR: { bg: "bg-amber-950/40", border: "border-amber-700/50", text: "text-amber-300", dot: "bg-amber-400" },
};

function LaneCard({ lane, data, err }) {
  const [showDetails, setShowDetails] = useState(false);

  if (err) {
    return (
      <div
        data-testid={`trade-flow-card-${lane}`}
        className="rounded-lg border border-rose-700/40 bg-rose-950/20 p-5"
      >
        <div className="font-mono text-xs uppercase tracking-widest text-rose-400">
          {lane}
        </div>
        <p className="mt-2 text-sm text-rose-200">Failed to load: {err}</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-5 text-xs text-zinc-500">
        {lane} — loading…
      </div>
    );
  }

  const h = data.headline || {};
  const status = h.status || "UNCLEAR";
  const colors = STATUS_COLORS[status] || STATUS_COLORS.UNCLEAR;

  const cadence = data.emission_cadence || {};
  const post = data.post_dry_run_outcomes || {};
  const checks = data.checks || {};
  const skipCats = post.submit_skip_categories || {};
  const samples = (post.samples || []).slice(0, 5);

  return (
    <div
      data-testid={`trade-flow-card-${lane}`}
      className={`rounded-lg border ${colors.border} ${colors.bg} p-5`}
    >
      {/* The answer in one line */}
      <div className="flex items-baseline justify-between gap-3">
        <div className="font-mono text-xs uppercase tracking-widest text-zinc-400">
          {lane}
        </div>
        <div className="flex items-center gap-2">
          <span className={`inline-block w-2.5 h-2.5 rounded-full ${colors.dot}`} />
          <span className={`text-xs font-semibold uppercase tracking-wider ${colors.text}`}>
            {status}
          </span>
        </div>
      </div>

      <div className="mt-4">
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          {status === "TRADING" ? "Status" : "Blocked by"}
        </div>
        <div
          data-testid={`trade-flow-headline-${lane}`}
          className={`mt-1 text-xl font-mono ${colors.text} break-words`}
        >
          {h.reason || "—"}
        </div>
        <p className="mt-3 text-sm text-zinc-300 leading-relaxed break-words">
          {h.detail || ""}
        </p>
      </div>

      {/* Fix command */}
      {h.fix && (
        <div className="mt-4 rounded border border-amber-700/40 bg-amber-950/30 p-3">
          <div className="text-[10px] uppercase tracking-widest text-amber-400 mb-1">
            Fix
          </div>
          <pre
            data-testid={`trade-flow-fix-${lane}`}
            className="whitespace-pre-wrap break-words text-xs text-amber-100 font-mono"
          >
            {h.fix}
          </pre>
        </div>
      )}

      {/* Counters */}
      <div className="mt-4 grid grid-cols-3 gap-2 text-center">
        <Counter label="Emitted 24h" value={cadence.total_intents ?? 0} />
        <Counter label="Cleared dry-run" value={post.dry_run_passed_count ?? 0} />
        <Counter
          label="Executed"
          value={post.executed_count ?? 0}
          accent={post.executed_count > 0 ? colors.text : "text-zinc-400"}
        />
      </div>

      {/* Details (collapsed by default) */}
      <button
        type="button"
        onClick={() => setShowDetails((v) => !v)}
        data-testid={`trade-flow-details-toggle-${lane}`}
        className="mt-4 text-xs text-zinc-500 hover:text-zinc-300"
      >
        {showDetails ? "▴ Hide" : "▾ Show"} evidence
      </button>

      {showDetails && (
        <div className="mt-3 space-y-4">
          {/* Per-check status */}
          <Section title="Prerequisite gates">
            {Object.entries(checks).map(([name, check]) => (
              <div
                key={name}
                data-testid={`trade-flow-check-${lane}-${name}`}
                className="flex items-baseline justify-between py-1 border-b border-zinc-800"
              >
                <span className="font-mono text-[11px] text-zinc-300">{name}</span>
                <span
                  className={`text-[11px] font-medium ${
                    check.ok ? "text-emerald-400" : "text-rose-400"
                  }`}
                >
                  {check.ok ? "pass" : "block"}
                </span>
              </div>
            ))}
          </Section>

          {/* Skip-category histogram (what's killing intents after dry-run) */}
          {Object.keys(skipCats).length > 0 && (
            <Section title="Post-dry-run blockers">
              {Object.entries(skipCats).map(([cat, count]) => (
                <div
                  key={cat}
                  className="flex items-baseline justify-between py-1 border-b border-zinc-800"
                >
                  <span className="font-mono text-[11px] text-rose-200">{cat}</span>
                  <span className="font-mono text-xs text-rose-100">{count}</span>
                </div>
              ))}
            </Section>
          )}

          {/* Sample intents */}
          {samples.length > 0 && (
            <Section title="Sample intents">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-zinc-500">
                    <th className="py-1 pr-1 text-left">Brain</th>
                    <th className="pr-1 text-left">Sym</th>
                    <th className="pr-1 text-left">Side</th>
                    <th className="pr-1 text-left">Conf</th>
                    <th className="pr-1 text-left">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {samples.map((s, i) => (
                    <tr
                      key={`${s.intent_id || i}-${i}`}
                      className="border-b border-zinc-900"
                    >
                      <td className="py-1 pr-1 text-zinc-300">{s.stack}</td>
                      <td className="pr-1 font-mono text-amber-400">{s.symbol}</td>
                      <td className="pr-1 text-emerald-400">{s.action}</td>
                      <td className="pr-1 font-mono text-zinc-300">
                        {s.confidence?.toFixed(2) ?? "—"}
                      </td>
                      <td className="pr-1 text-rose-300 break-all">
                        {s.skip_category || s.outcome}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
          )}
        </div>
      )}
    </div>
  );
}

function Counter({ label, value, accent = "text-zinc-100" }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/60 px-2 py-2">
      <div className={`text-lg font-mono ${accent}`}>{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">
        {label}
      </div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1">
        {title}
      </div>
      {children}
    </div>
  );
}

export default function TradeFlow() {
  const [data, setData] = useState({ equity: null, crypto: null });
  const [errs, setErrs] = useState({ equity: null, crypto: null });
  const [loading, setLoading] = useState(true);
  const [lastFetch, setLastFetch] = useState(null);

  const load = useCallback(async () => {
    const next = { equity: null, crypto: null };
    const nextErr = { equity: null, crypto: null };
    await Promise.all(
      LANES.map(async (lane) => {
        try {
          const res = await api.get(
            `/admin/lane-readiness/${lane}?hours=24`,
          );
          next[lane] = res.data;
        } catch (e) {
          nextErr[lane] =
            e?.response?.data?.detail || e.message || "fetch failed";
        }
      }),
    );
    setData(next);
    setErrs(nextErr);
    setLoading(false);
    setLastFetch(new Date());
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="reveal" data-testid="trade-flow-page">
      <PageHeader
        eyebrow="Operator"
        title="Trade Flow"
        sub="Why is each lane trading or not? One answer per lane, evidence on tap."
        right={
          <button
            type="button"
            onClick={load}
            data-testid="trade-flow-refresh"
            className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
          >
            refresh
          </button>
        }
        testid="trade-flow-header"
      />

      {loading ? (
        <p className="mt-4 text-sm text-zinc-500">loading…</p>
      ) : (
        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          {LANES.map((lane) => (
            <LaneCard
              key={lane}
              lane={lane}
              data={data[lane]}
              err={errs[lane]}
            />
          ))}
        </div>
      )}

      {lastFetch && (
        <p className="mt-4 text-[10px] uppercase tracking-widest text-zinc-600">
          refreshed {lastFetch.toLocaleTimeString()} · auto-refresh every{" "}
          {POLL_MS / 1000}s
        </p>
      )}
    </div>
  );
}
