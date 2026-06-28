/**
 * Seat Context — cleaned, read-only witness context for the Seat.
 *
 * Doctrine pin (TRIAL COURT, NOT A VOTING SYSTEM):
 *     The Witnesses page (/admin/witnesses) shows EVERYTHING raw.
 *     This page shows only what survived RoadGuard label filtering
 *     (no SPAM, no DUPLICATE_BURST, no FLIP_FLOP, no
 *     SOFT_NEWS_CLUSTER, no SOURCE_DRIFT).
 *
 *     Even cleaned, rows here are STILL `verifier_status=UNTRUSTED`
 *     and `influence_allowed=False`. The Seat is INFORMED by these,
 *     not directed by them. There is no click-to-execute path.
 *     There is no "act on this" button. There is no "promote this"
 *     button. Verifier alone owns those transitions.
 *
 *     The filter-audit section is intentional: the operator deserves
 *     to see how aggressively RoadGuard is dropping signals, not
 *     to discover later that 60% of NVDA witnesses got silently
 *     binned. Transparency is the safety net.
 */
import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/ui-bits";

const POLL_MS = 60_000;

const SIDE_COLORS = {
  BUY:  { text: "text-emerald-400", bg: "bg-emerald-950/40" },
  SELL: { text: "text-rose-400",    bg: "bg-rose-950/40" },
  HOLD: { text: "text-zinc-400",    bg: "bg-zinc-900/40" },
};

const LABEL_COPY = {
  EXTERNAL_SIGNAL_SPAM:              "spam (rate/symbol burst)",
  EXTERNAL_SIGNAL_DUPLICATE_BURST:   "duplicate burst",
  EXTERNAL_SIGNAL_FLIP_FLOP:         "direction flip-flop",
  EXTERNAL_SIGNAL_SOFT_NEWS_CLUSTER: "soft-news cluster",
  EXTERNAL_SIGNAL_SOURCE_DRIFT:      "source drift",
};

function Banner() {
  return (
    <div
      data-testid="seat-context-banner"
      className="mt-4 rounded-md border border-indigo-900/60 bg-indigo-950/30 p-3 text-xs text-zinc-400"
    >
      <span className="font-mono uppercase tracking-widest text-indigo-400">
        Seat-bound · cleaned context ·
      </span>{" "}
      Rows shown here survived RoadGuard label filtering. They are still{" "}
      <span className="font-mono text-zinc-300">UNTRUSTED</span> and{" "}
      <span className="font-mono text-zinc-300">influence_allowed=False</span>.
      The Seat is{" "}
      <span className="font-semibold text-zinc-300">informed</span> by these,
      not directed by them. No click-to-execute path. No promotion controls.
      Verifier owns trust transitions.
    </div>
  );
}

function FilterAudit({ totals, byLabel }) {
  const filtered = totals?.filtered_out ?? 0;
  const total = totals?.total_in_window ?? 0;
  const pct = total > 0 ? Math.round((filtered / total) * 100) : 0;
  return (
    <div
      data-testid="seat-context-filter-audit"
      className="mt-6 rounded-md border border-zinc-800 bg-zinc-950/40 p-3"
    >
      <div className="mb-2 text-[10px] uppercase tracking-widest text-zinc-500">
        RoadGuard filter audit
      </div>
      <div className="flex flex-wrap gap-4 text-xs">
        <div>
          <span className="text-zinc-500">total in window</span>{" "}
          <span className="ml-1 font-mono text-zinc-200">{total}</span>
        </div>
        <div>
          <span className="text-zinc-500">cleaned shown</span>{" "}
          <span className="ml-1 font-mono text-emerald-400">
            {totals?.cleaned_shown ?? 0}
          </span>
        </div>
        <div>
          <span className="text-zinc-500">filtered out</span>{" "}
          <span className="ml-1 font-mono text-amber-400">
            {filtered} ({pct}%)
          </span>
        </div>
      </div>
      {Object.keys(byLabel || {}).length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2 text-[10px]">
          {Object.entries(byLabel).map(([label, n]) => (
            <span
              key={label}
              data-testid={`seat-context-filter-${label}`}
              className="rounded border border-amber-900/40 bg-amber-950/30 px-2 py-1 font-mono text-amber-300"
            >
              {LABEL_COPY[label] || label.toLowerCase()}{" "}
              <span className="text-amber-400">×{n}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function CleanRow({ row }) {
  const side = SIDE_COLORS[row.side] || SIDE_COLORS.HOLD;
  const ts = row.received_at
    ? new Date(row.received_at).toLocaleString(undefined, {
        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      })
    : "—";
  return (
    <div
      data-testid={`seat-context-row-${row.id}`}
      className="rounded-md border border-zinc-800 bg-zinc-950/40 p-3 opacity-80"
    >
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-mono text-zinc-200">{row.symbol}</span>
        <span className={`rounded px-1.5 py-0.5 font-mono ${side.text} ${side.bg}`}>
          {row.side}
        </span>
        <span className="font-mono text-zinc-500">· {row.source} · {row.event || "—"}</span>
        <span className="ml-auto inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[10px] uppercase tracking-widest text-indigo-400">
          <span className="h-1.5 w-1.5 rounded-full bg-indigo-400" />
          read-only · advisory
        </span>
      </div>
      {row.reason && (
        <p className="mt-2 text-xs leading-relaxed text-zinc-400">
          {row.reason}
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-3 text-[10px] uppercase tracking-widest text-zinc-600">
        <span>received: <span className="font-mono text-zinc-500">{ts}</span></span>
      </div>
    </div>
  );
}

export default function SeatContext() {
  const [data, setData] = useState({
    items: [],
    totals: {},
    filtered_by_label: {},
  });
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastFetch, setLastFetch] = useState(null);
  const [symbol, setSymbol] = useState("");

  const load = useCallback(async () => {
    try {
      const url = symbol
        ? `/admin/external-signals/seat-context?hours=24&limit=50&symbol=${encodeURIComponent(symbol)}`
        : "/admin/external-signals/seat-context?hours=24&limit=50";
      const r = await api.get(url);
      setData({
        items: r.data.items || [],
        totals: r.data.totals || {},
        filtered_by_label: r.data.filtered_by_label || {},
      });
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message || "fetch failed");
    } finally {
      setLoading(false);
      setLastFetch(new Date());
    }
  }, [symbol]);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="reveal" data-testid="seat-context-page">
      <PageHeader
        eyebrow="Operator · Seat"
        title="Seat Context"
        sub="Cleaned witness context — read-only advisory. No execution path here."
        right={
          <div className="flex items-center gap-2">
            <input
              data-testid="seat-context-symbol-filter"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="symbol filter (e.g. NVDA)"
              className="rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 placeholder:text-zinc-600"
            />
            <button
              type="button"
              onClick={load}
              data-testid="seat-context-refresh"
              className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
            >
              refresh
            </button>
          </div>
        }
        testid="seat-context-header"
      />

      <Banner />

      {err && (
        <div
          data-testid="seat-context-error"
          className="mt-4 rounded-md border border-rose-800 bg-rose-950/40 p-3 text-xs text-rose-300"
        >
          {err}
        </div>
      )}

      <FilterAudit totals={data.totals} byLabel={data.filtered_by_label} />

      <div className="mt-6">
        <div className="mb-2 text-[10px] uppercase tracking-widest text-zinc-500">
          Cleaned witness rows · last 24h{symbol ? ` · ${symbol}` : ""}
        </div>
        {loading ? (
          <p className="text-sm text-zinc-500">loading…</p>
        ) : data.items.length === 0 ? (
          <div
            data-testid="seat-context-empty"
            className="rounded-md border border-zinc-800 bg-zinc-950/40 p-4 text-xs text-zinc-500"
          >
            No cleaned witnesses in the window
            {symbol ? ` for ${symbol}` : ""}.
            {data.totals?.filtered_out > 0 && (
              <span className="ml-2 text-amber-400">
                ({data.totals.filtered_out} signals were filtered out by RoadGuard.)
              </span>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-2">
            {data.items.map((row) => (
              <CleanRow key={row.id} row={row} />
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
