/**
 * NativeBrainRuntimeTile — operator visibility for the in-process
 * brain migration (2026-02-23).
 *
 * One row per brain (Barracuda, GTO, Camino, Hellcat). Each row shows:
 *   * enabled flag (lights up GREEN when on, slate when dormant)
 *   * SILENT badge (red) when enabled + no tick in 5+ minutes —
 *     the doctrinal "silent-worker bug" signal the operator built
 *     this migration to kill
 *   * tick age (sec since last tick)
 *   * tick count in last 60m
 *   * emit count in last 60m
 *   * error count in last 60m
 *
 * Polls /api/admin/native-runtime/status every 20s. Read-only. No
 * actions, no toggles — flag flips happen via env vars on prod, not
 * via the UI (intentional: the toggle is operator-driven and audited
 * at the deploy layer).
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 20_000;

const ROW_LABEL = {
  barracuda: "Barracuda",
  gto: "GTO",
  camino: "Camino",
  hellcat: "Hellcat",
};

function fmtAge(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

export default function NativeBrainRuntimeTile() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await api.get("/admin/native-runtime/status");
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

  const brains = data?.brains || [];
  const silentCount = data?.silent_brains?.length || 0;
  const enabledCount = data?.enabled_brains?.length || 0;

  return (
    <div
      data-testid="native-brain-runtime-tile"
      className="rounded-lg border border-slate-700 bg-slate-900/50 p-4 space-y-3"
    >
      <div className="flex items-baseline justify-between">
        <div>
          <h3 className="text-base font-semibold text-slate-100">
            Native Brain Runtimes
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            In-process brains. Silent =&nbsp;enabled but no tick in 5+
            min — the exact symptom the migration kills.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          data-testid="native-brain-runtime-refresh"
          className="text-xs text-slate-400 hover:text-slate-200 inline-flex items-center gap-1"
          aria-label="refresh"
        >
          <ArrowsClockwise size={14} weight="bold" />
          refresh
        </button>
      </div>

      <div className="flex items-center gap-4 text-xs">
        <span className="text-slate-400">
          enabled:{" "}
          <span
            data-testid="native-brain-runtime-enabled-count"
            className={enabledCount > 0 ? "text-emerald-400 font-semibold" : "text-slate-500"}
          >
            {enabledCount} / 4
          </span>
        </span>
        <span className="text-slate-400">
          silent:{" "}
          <span
            data-testid="native-brain-runtime-silent-count"
            className={silentCount > 0 ? "text-red-400 font-semibold" : "text-slate-500"}
          >
            {silentCount}
          </span>
        </span>
      </div>

      {loading && !data && (
        <div className="text-xs text-slate-500">loading…</div>
      )}
      {err && (
        <div
          data-testid="native-brain-runtime-error"
          className="text-xs text-red-400"
        >
          {String(err)}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-400 border-b border-slate-800">
              <th className="text-left py-1.5 pr-3">brain</th>
              <th className="text-left py-1.5 pr-3">flag</th>
              <th className="text-right py-1.5 pr-3">last tick</th>
              <th className="text-right py-1.5 pr-3">ticks/60m</th>
              <th className="text-right py-1.5 pr-3">emitted/60m</th>
              <th className="text-right py-1.5 pr-3">errors/60m</th>
              <th className="text-left py-1.5">status</th>
            </tr>
          </thead>
          <tbody>
            {brains.map((b) => {
              const tone = b.silent
                ? "text-red-300"
                : b.enabled
                  ? "text-emerald-300"
                  : "text-slate-300";
              return (
                <tr
                  key={b.brain_id}
                  data-testid={`native-brain-row-${b.brain_id}`}
                  className="border-b border-slate-800/60"
                >
                  <td className="py-1.5 pr-3 font-mono">
                    {ROW_LABEL[b.brain_id] || b.brain_id}
                  </td>
                  <td className="py-1.5 pr-3">
                    <span
                      className={
                        b.enabled
                          ? "px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-300 text-[10px]"
                          : "px-1.5 py-0.5 rounded bg-slate-800 text-slate-500 text-[10px]"
                      }
                      data-testid={`native-brain-flag-${b.brain_id}`}
                    >
                      {b.enabled ? "ON" : "off"}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-right font-mono">
                    {fmtAge(b.tick_age_sec)}
                  </td>
                  <td className="py-1.5 pr-3 text-right font-mono">
                    {b.tick_count_60m}
                  </td>
                  <td className="py-1.5 pr-3 text-right font-mono">
                    {b.emitted_60m}
                  </td>
                  <td
                    className={`py-1.5 pr-3 text-right font-mono ${
                      b.errors_60m > 0 ? "text-amber-300" : ""
                    }`}
                  >
                    {b.errors_60m}
                  </td>
                  <td className={`py-1.5 ${tone}`}>
                    {b.silent ? (
                      <span
                        data-testid={`native-brain-silent-${b.brain_id}`}
                        className="px-1.5 py-0.5 rounded bg-red-900/40 text-red-300 text-[10px] font-semibold"
                      >
                        SILENT
                      </span>
                    ) : b.enabled ? (
                      "ticking"
                    ) : (
                      "dormant"
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {data?.as_of && (
        <p className="text-[10px] text-slate-500 font-mono">
          as of {data.as_of}
        </p>
      )}
    </div>
  );
}
