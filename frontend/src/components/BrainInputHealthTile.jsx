/**
 * BrainInputHealthTile — operator visibility into instrument quality.
 *
 * Answers the operator concern (2026-02-23):
 *   "I'm fine with intents but I can't know which one is good to go
 *    if the instruments are failing to report accurate information."
 *
 * Two stacked sub-tables:
 *
 *   1. Per-brain summary — last emit age, 24h count, 7d count,
 *      directional %, % of universe each brain can EVALUATE today
 *      (fresh snapshot + bars ≥ 60 + required fields present).
 *      Brain with last_emit_age >> peers stands out instantly —
 *      this is the "Barracuda stopped 4 days ago" detector.
 *
 *   2. Per-symbol universe — snapshot age, bar count, RSI, close,
 *      and which brains' required-field contracts the snapshot
 *      satisfies. Filter to "problems only" so 50 healthy rows
 *      don't drown the few broken ones.
 *
 * Polls /api/admin/brain-input-health every 30s. Read-only.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowsClockwise } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 30_000;

const BRAIN_LABEL = {
  barracuda: "Barracuda",
  gto: "GTO",
  camino: "Camino",
  hellcat: "Hellcat",
};


function fmtAge(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`;
  return `${(sec / 86400).toFixed(1)}d`;
}

function emitToneFromAge(sec) {
  if (sec == null) return "text-red-300";
  if (sec < 3600) return "text-emerald-300";
  if (sec < 6 * 3600) return "text-amber-300";
  return "text-red-300";
}

export default function BrainInputHealthTile() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [problemsOnly, setProblemsOnly] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await api.get("/admin/brain-input-health");
      setData(res.data);
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message || "fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  const universeRows = useMemo(() => {
    const rows = data?.universe || [];
    if (!problemsOnly) return rows;
    return rows.filter((r) =>
      !r.has_snapshot || r.stale || r.bars_thin || (r.missing_for?.length || 0) > 0,
    );
  }, [data, problemsOnly]);

  const summary = data?.summary;

  return (
    <div
      data-testid="brain-input-health-tile"
      className="rounded-lg border border-slate-700 bg-slate-900/50 p-4 space-y-4"
    >
      <div className="flex items-baseline justify-between">
        <div>
          <h3 className="text-base font-semibold text-slate-100">
            Brain Input Health
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            Trust filter for intents. Fresh = snapshot &lt;
            {data?.stale_threshold_sec ? ` ${Math.floor(data.stale_threshold_sec / 60)}m` : ""},
            bars ≥ {data?.min_reliable_bars ?? 60}, all required fields
            present.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          data-testid="brain-input-health-refresh"
          className="text-xs text-slate-400 hover:text-slate-200 inline-flex items-center gap-1"
          aria-label="refresh"
        >
          <ArrowsClockwise size={14} weight="bold" />
          refresh
        </button>
      </div>

      {loading && !data && (
        <div className="text-xs text-slate-500">loading…</div>
      )}
      {err && (
        <div
          data-testid="brain-input-health-error"
          className="text-xs text-red-400"
        >
          {String(err)}
        </div>
      )}

      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-2 text-xs">
          <Stat label="universe" value={summary.universe_size} testId="bih-summary-universe" />
          <Stat
            label="fresh"
            value={summary.fresh_count}
            tone={summary.fresh_count === summary.universe_size ? "emerald" : "amber"}
            testId="bih-summary-fresh"
          />
          <Stat
            label="stale"
            value={summary.stale_count}
            tone={summary.stale_count === 0 ? "slate" : "amber"}
            testId="bih-summary-stale"
          />
          <Stat
            label="no snapshot"
            value={summary.missing_snapshot_count}
            tone={summary.missing_snapshot_count === 0 ? "slate" : "red"}
            testId="bih-summary-missing"
          />
          <Stat
            label="thin bars"
            value={summary.thin_bars_count}
            tone={summary.thin_bars_count === 0 ? "slate" : "amber"}
            testId="bih-summary-thin"
          />
          <Stat
            label="evaluable all 4"
            value={summary.evaluable_all_brains_count}
            tone={
              summary.evaluable_all_brains_count === summary.universe_size
                ? "emerald"
                : "amber"
            }
            testId="bih-summary-eval-all"
          />
        </div>
      )}

      {/* Per-brain summary table */}
      <div>
        <div className="text-xs font-semibold text-slate-300 mb-1.5">
          Brains
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-400 border-b border-slate-800">
                <th className="text-left py-1.5 pr-3">brain</th>
                <th className="text-left py-1.5 pr-3">doctrine</th>
                <th className="text-right py-1.5 pr-3">last emit</th>
                <th className="text-right py-1.5 pr-3">last directional</th>
                <th className="text-right py-1.5 pr-3">emits 24h</th>
                <th className="text-right py-1.5 pr-3">emits 7d</th>
                <th className="text-right py-1.5 pr-3">dir % 7d</th>
                <th className="text-right py-1.5">evaluable</th>
              </tr>
            </thead>
            <tbody>
              {(data?.brains || []).map((b) => (
                <tr
                  key={b.brain_id}
                  data-testid={`bih-brain-row-${b.brain_id}`}
                  className="border-b border-slate-800/60"
                >
                  <td className="py-1.5 pr-3 font-mono">
                    {BRAIN_LABEL[b.brain_id] || b.brain_id}
                  </td>
                  <td className="py-1.5 pr-3 text-slate-400">{b.doctrine}</td>
                  <td
                    className={`py-1.5 pr-3 text-right font-mono ${emitToneFromAge(
                      b.last_emit_age_sec,
                    )}`}
                    data-testid={`bih-brain-${b.brain_id}-last-emit`}
                  >
                    {fmtAge(b.last_emit_age_sec)}
                  </td>
                  <td
                    className={`py-1.5 pr-3 text-right font-mono ${emitToneFromAge(
                      b.last_directional_age_sec,
                    )}`}
                  >
                    {fmtAge(b.last_directional_age_sec)}
                  </td>
                  <td className={`py-1.5 pr-3 text-right font-mono ${b.emits_24h === 0 ? "text-red-300" : ""}`}>
                    {b.emits_24h}
                  </td>
                  <td className="py-1.5 pr-3 text-right font-mono">
                    {b.emits_7d}
                  </td>
                  <td className="py-1.5 pr-3 text-right font-mono">
                    {b.directional_pct_7d == null ? "—" : `${b.directional_pct_7d}%`}
                  </td>
                  <td className="py-1.5 text-right font-mono text-slate-300">
                    {b.evaluable_count} ({b.evaluable_pct}%)
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Per-symbol universe */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <div className="text-xs font-semibold text-slate-300">
            Universe ({universeRows.length}
            {problemsOnly ? " problem" : ""}
            {universeRows.length === 1 ? "" : "s"})
          </div>
          <label className="text-xs text-slate-400 inline-flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={problemsOnly}
              onChange={(e) => setProblemsOnly(e.target.checked)}
              data-testid="bih-problems-only-toggle"
              className="accent-emerald-500"
            />
            problems only
          </label>
        </div>
        <div className="overflow-x-auto max-h-96 overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-slate-900">
              <tr className="text-slate-400 border-b border-slate-800">
                <th className="text-left py-1.5 pr-3">symbol</th>
                <th className="text-right py-1.5 pr-3">snap age</th>
                <th className="text-right py-1.5 pr-3">bars</th>
                <th className="text-right py-1.5 pr-3">RSI</th>
                <th className="text-right py-1.5 pr-3">close</th>
                <th className="text-left py-1.5 pr-3">source</th>
                <th className="text-left py-1.5">blocks brain</th>
              </tr>
            </thead>
            <tbody>
              {universeRows.map((r) => {
                const ageTone = r.stale ? "text-red-300" : "text-slate-300";
                return (
                  <tr
                    key={r.symbol}
                    data-testid={`bih-symbol-row-${r.symbol}`}
                    className="border-b border-slate-800/40"
                  >
                    <td className="py-1.5 pr-3 font-mono">{r.symbol}</td>
                    <td className={`py-1.5 pr-3 text-right font-mono ${ageTone}`}>
                      {r.has_snapshot ? fmtAge(r.snapshot_age_sec) : (
                        <span className="text-red-300">NO SNAP</span>
                      )}
                    </td>
                    <td
                      className={`py-1.5 pr-3 text-right font-mono ${r.bars_thin ? "text-amber-300" : ""}`}
                    >
                      {r.bars_seen ?? "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono">
                      {r.rsi14 != null ? r.rsi14.toFixed(1) : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono">
                      {r.last_close != null ? r.last_close.toFixed(2) : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-slate-500 text-[10px]">
                      {r.source || "—"}
                    </td>
                    <td className="py-1.5">
                      {(r.missing_for || []).length === 0 ? (
                        <span className="text-emerald-400 text-[10px]">all 4 ok</span>
                      ) : (
                        <span
                          className="text-amber-300 text-[10px] font-mono"
                          data-testid={`bih-missing-for-${r.symbol}`}
                        >
                          {r.missing_for.join(", ")}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
              {universeRows.length === 0 && (
                <tr>
                  <td colSpan={7} className="py-3 text-center text-slate-500">
                    no problem symbols — all instruments healthy
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {data?.as_of && (
        <p className="text-[10px] text-slate-500 font-mono">
          as of {data.as_of}
        </p>
      )}
    </div>
  );
}

function Stat({ label, value, tone = "slate", testId }) {
  const toneMap = {
    emerald: "text-emerald-300",
    amber: "text-amber-300",
    red: "text-red-300",
    slate: "text-slate-200",
  };
  return (
    <div className="bg-slate-950/40 rounded border border-slate-800 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div
        className={`text-lg font-semibold font-mono ${toneMap[tone] || toneMap.slate}`}
        data-testid={testId}
      >
        {value}
      </div>
    </div>
  );
}
