/**
 * Witnesses — read-only "Untrusted Witnesses" panel.
 *
 * Doctrine pin (2026-02-23, witness-council layer):
 *     TRIAL COURT, NOT A VOTING SYSTEM.
 *
 *     External signals (Polygon news+sentiment today, eventually
 *     Pine / Public / MTR) land in the holding cell DEFAULT-HOSTILE:
 *         verifier_status   = UNTRUSTED
 *         influence_allowed = False
 *     Governor returns 0.0 modifier for any witness where
 *     influence_allowed=False. Nothing on this page can move a trade.
 *
 *     The page renders witnesses DIMMED and READ-ONLY. There is no
 *     click-to-execute path. There is no "promote this signal"
 *     button. The Verifier is the only thing that can ever change
 *     a source's status — and the Verifier is a future component,
 *     not a UI control.
 *
 *     The page exists so the operator can SEE what witnesses are
 *     saying — situational awareness, not authority.
 */
import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/ui-bits";

const POLL_MS = 60_000;

const STATUS_BADGE = {
  UNTRUSTED: {
    bg: "bg-zinc-800/60",
    border: "border-zinc-700",
    text: "text-zinc-400",
    dot: "bg-zinc-500",
    label: "UNTRUSTED",
    sub: "no execution influence",
  },
  WATCHLIST: {
    bg: "bg-amber-950/40",
    border: "border-amber-800/60",
    text: "text-amber-300",
    dot: "bg-amber-400",
    label: "WATCHLIST",
    sub: "proving period — still no influence",
  },
  TRUSTED: {
    bg: "bg-emerald-950/40",
    border: "border-emerald-800/60",
    text: "text-emerald-300",
    dot: "bg-emerald-400",
    label: "TRUSTED",
    sub: "Verifier-promoted; small modifier may apply on orthogonal signals",
  },
};

const SIDE_COLORS = {
  BUY:  { text: "text-emerald-400", bg: "bg-emerald-950/40" },
  SELL: { text: "text-rose-400",    bg: "bg-rose-950/40" },
  HOLD: { text: "text-zinc-400",    bg: "bg-zinc-900/40" },
};

function Banner() {
  return (
    <div
      data-testid="witnesses-doctrine-banner"
      className="mt-4 rounded-md border border-zinc-800 bg-zinc-950/60 p-3 text-xs text-zinc-400"
    >
      <span className="font-mono uppercase tracking-widest text-zinc-500">
        Doctrine ·
      </span>{" "}
      Witnesses are{" "}
      <span className="font-semibold text-zinc-300">default-hostile</span>.
      Pine, Polygon, Public, MTR all enter as{" "}
      <span className="font-mono text-zinc-300">UNTRUSTED</span> with{" "}
      <span className="font-mono text-zinc-300">influence_allowed=False</span>.
      The Governor returns 0.0 modifier for any witness where{" "}
      <span className="font-mono">influence_allowed</span> is false — regardless
      of self-reported confidence. Verifier (future) decides if any source
      ever earns weight via the four-phase progression. Until then, this
      panel is situational awareness only.
    </div>
  );
}

function CredibilityLedger({ rows }) {
  if (!rows || rows.length === 0) {
    return (
      <div
        data-testid="witnesses-credibility-empty"
        className="rounded-md border border-zinc-800 bg-zinc-950/40 p-4 text-xs text-zinc-500"
      >
        No credibility ledger entries yet. The first witness signal
        of any source will create a default-hostile case file here.
      </div>
    );
  }
  return (
    <div className="overflow-hidden rounded-md border border-zinc-800">
      <table className="w-full text-xs" data-testid="witnesses-credibility-table">
        <thead className="bg-zinc-900/60 text-zinc-500 uppercase tracking-widest text-[10px]">
          <tr>
            <th className="px-3 py-2 text-left">source</th>
            <th className="px-3 py-2 text-left">status</th>
            <th className="px-3 py-2 text-right">samples</th>
            <th className="px-3 py-2 text-right">wins</th>
            <th className="px-3 py-2 text-right">losses</th>
            <th className="px-3 py-2 text-right">verified_alpha</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-800/50 text-zinc-300">
          {rows.map((row) => {
            const badge = STATUS_BADGE[row.status] || STATUS_BADGE.UNTRUSTED;
            return (
              <tr key={row.source} data-testid={`witnesses-credibility-${row.source}`}>
                <td className="px-3 py-2 font-mono text-zinc-200">{row.source}</td>
                <td className="px-3 py-2">
                  <span
                    className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 ${badge.bg} ${badge.text} border ${badge.border}`}
                  >
                    <span className={`h-1.5 w-1.5 rounded-full ${badge.dot}`} />
                    {badge.label}
                  </span>
                </td>
                <td className="px-3 py-2 text-right text-zinc-400">{row.samples ?? 0}</td>
                <td className="px-3 py-2 text-right text-emerald-400/70">{row.wins ?? 0}</td>
                <td className="px-3 py-2 text-right text-rose-400/70">{row.losses ?? 0}</td>
                <td className="px-3 py-2 text-right font-mono text-zinc-400">
                  {(row.verified_alpha ?? 0).toFixed(4)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function WitnessRow({ row }) {
  const badge = STATUS_BADGE[row.verifier_status] || STATUS_BADGE.UNTRUSTED;
  const side = SIDE_COLORS[row.side] || SIDE_COLORS.HOLD;
  const ts = row.received_at
    ? new Date(row.received_at).toLocaleString(undefined, {
        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      })
    : "—";
  return (
    <div
      data-testid={`witness-row-${row.id || row.dedup_key}`}
      className={`rounded-md border ${badge.border} ${badge.bg} p-3 transition-opacity ${
        row.influence_allowed ? "opacity-100" : "opacity-70"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-mono text-zinc-200">{row.symbol}</span>
        <span className={`rounded px-1.5 py-0.5 font-mono ${side.text} ${side.bg}`}>
          {row.side}
        </span>
        <span className="font-mono text-zinc-500">·</span>
        <span className="font-mono text-zinc-500">{row.source}</span>
        <span className="font-mono text-zinc-500">·</span>
        <span className="font-mono text-zinc-500">{row.event || "—"}</span>
        <span className="ml-auto inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-[10px] uppercase tracking-widest"
              style={{ borderColor: "transparent" }}>
          <span className={`h-1.5 w-1.5 rounded-full ${badge.dot}`} />
          <span className={badge.text}>{badge.label}</span>
        </span>
      </div>
      {row.reason && (
        <p className="mt-2 text-xs leading-relaxed text-zinc-400">
          {row.reason}
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-3 text-[10px] uppercase tracking-widest text-zinc-600">
        <span>self-reported: <span className="font-mono text-zinc-500">
          {(row.self_reported_confidence ?? 0).toFixed(2)} <span className="lowercase tracking-normal italic">advisory</span>
        </span></span>
        <span>received: <span className="font-mono text-zinc-500">{ts}</span></span>
      </div>
    </div>
  );
}

function SourceTotals({ totals, bySource }) {
  if (!bySource || Object.keys(bySource).length === 0) return null;
  return (
    <div data-testid="witnesses-source-totals" className="mt-4 flex flex-wrap gap-3">
      {Object.entries(bySource).map(([src, sides]) => {
        const total = Object.values(sides).reduce((a, b) => a + b, 0);
        return (
          <div
            key={src}
            className="rounded-md border border-zinc-800 bg-zinc-950/40 px-3 py-2 text-xs"
            data-testid={`witnesses-source-total-${src}`}
          >
            <div className="font-mono text-zinc-300">{src}</div>
            <div className="mt-1 flex gap-2 text-[10px] uppercase tracking-widest text-zinc-500">
              <span>total <span className="font-mono text-zinc-300">{total}</span></span>
              {sides.BUY  ? <span className="text-emerald-500">BUY {sides.BUY}</span>  : null}
              {sides.SELL ? <span className="text-rose-500">SELL {sides.SELL}</span>   : null}
              {sides.HOLD ? <span className="text-zinc-500">HOLD {sides.HOLD}</span>   : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function Witnesses() {
  const [signals, setSignals] = useState({ items: [], totals: {}, by_source: {} });
  const [credibility, setCredibility] = useState([]);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastFetch, setLastFetch] = useState(null);

  const load = useCallback(async () => {
    try {
      const [a, b] = await Promise.all([
        api.get("/admin/external-signals?hours=24&limit=100"),
        api.get("/admin/external-signals/credibility"),
      ]);
      setSignals({
        items: a.data.items || [],
        totals: a.data.totals || {},
        by_source: a.data.by_source || {},
      });
      setCredibility(b.data.items || []);
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message || "fetch failed");
    } finally {
      setLoading(false);
      setLastFetch(new Date());
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="reveal" data-testid="witnesses-page">
      <PageHeader
        eyebrow="Operator"
        title="Witnesses"
        sub="External signals — default-hostile. Nothing here moves a trade."
        right={
          <button
            type="button"
            onClick={load}
            data-testid="witnesses-refresh"
            className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
          >
            refresh
          </button>
        }
        testid="witnesses-header"
      />

      <Banner />

      {err && (
        <div
          data-testid="witnesses-error"
          className="mt-4 rounded-md border border-rose-800 bg-rose-950/40 p-3 text-xs text-rose-300"
        >
          {err}
        </div>
      )}

      <div className="mt-6">
        <div className="mb-2 text-[10px] uppercase tracking-widest text-zinc-500">
          Credibility ledger · Verifier&apos;s case file
        </div>
        <CredibilityLedger rows={credibility} />
      </div>

      <SourceTotals totals={signals.totals} bySource={signals.by_source} />

      <div className="mt-6">
        <div className="mb-2 flex items-baseline gap-3">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500">
            Recent witnesses · last 24h
          </div>
          <div className="text-[10px] text-zinc-600">
            {signals.totals?.total_in_window ?? 0} shown ·{" "}
            {signals.totals?.total_24h ?? 0} total in last 24h
          </div>
        </div>
        {loading ? (
          <p className="text-sm text-zinc-500">loading…</p>
        ) : signals.items.length === 0 ? (
          <div
            data-testid="witnesses-empty"
            className="rounded-md border border-zinc-800 bg-zinc-950/40 p-4 text-xs text-zinc-500"
          >
            No witness signals in the window. The Polygon news worker
            ticks hourly; rows will appear as articles are published.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-2">
            {signals.items.map((row) => (
              <WitnessRow key={row.id || row.dedup_key} row={row} />
            ))}
          </div>
        )}
      </div>

      {lastFetch && (
        <p className="mt-4 text-[10px] uppercase tracking-widest text-zinc-600">
          refreshed {lastFetch.toLocaleTimeString()} · auto-refresh every{" "}
          {POLL_MS / 1000}s
        </p>
      )}
    </div>
  );
}
