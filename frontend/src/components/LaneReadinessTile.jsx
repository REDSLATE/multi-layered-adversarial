/**
 * LaneReadinessTile — "why isn't this lane trading?" surface.
 *
 * Reads `/api/admin/lane-readiness/{equity|crypto}` and renders the
 * answer human-readably: per-check pass/fail with operator fix command,
 * the emission cadence, the **post-dry-run skip categories** (the field
 * that names the actual gate killing intents after dry-run passes),
 * and the top failed dry-run gates.
 *
 * Built 2026-06-26 after the operator reported "equity isn't trading
 * and I can't read curl on my phone." This tile is the answer to that.
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 30_000;
const LANES = ["equity", "crypto"];

function StatusDot({ ok }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${ok ? "bg-emerald-400" : "bg-rose-500"}`}
    />
  );
}

function CheckRow({ name, check }) {
  return (
    <div
      data-testid={`lane-readiness-check-${name}`}
      className="border-b border-zinc-800 py-2"
    >
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex items-center gap-2">
          <StatusDot ok={check.ok} />
          <span className="font-mono text-xs uppercase tracking-wider text-zinc-200">
            {name}
          </span>
        </div>
        <span
          className={`text-xs font-medium ${
            check.ok ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          {check.ok ? "PASS" : "BLOCK"}
        </span>
      </div>
      <p className="mt-1 ml-4 text-xs text-zinc-400 break-words">
        {check.detail}
      </p>
      {!check.ok && check.fix && (
        <pre className="mt-2 ml-4 whitespace-pre-wrap rounded border border-amber-700/40 bg-amber-950/20 p-2 text-xs text-amber-200">
          FIX: {check.fix}
        </pre>
      )}
    </div>
  );
}

function LaneCard({ lane, data, err }) {
  if (err) {
    return (
      <div
        data-testid={`lane-readiness-card-${lane}`}
        className="rounded border border-rose-700/50 bg-rose-950/20 p-3"
      >
        <div className="text-xs uppercase tracking-wider text-rose-300">
          {lane} — fetch failed
        </div>
        <p className="mt-2 text-xs text-rose-200">{err}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="rounded border border-zinc-800 bg-zinc-900/40 p-3 text-xs text-zinc-500">
        {lane} — loading…
      </div>
    );
  }

  const checks = data.checks || {};
  const cadence = data.emission_cadence || {};
  const post = data.post_dry_run_outcomes || {};
  const topBlocks = data.top_block_reasons || [];
  const ready = data.ready_to_trade;
  const skipCats = post.submit_skip_categories || {};
  const skipCatEntries = Object.entries(skipCats);
  const samples = (post.samples || []).slice(0, 5);

  return (
    <div
      data-testid={`lane-readiness-card-${lane}`}
      className="rounded border border-zinc-800 bg-zinc-900/40 p-4 space-y-4"
    >
      <div className="flex items-baseline justify-between">
        <div>
          <div className="font-mono text-xs uppercase tracking-wider text-zinc-400">
            {lane}
          </div>
          <div className="mt-0.5 text-xs text-zinc-500">
            {cadence.total_intents ?? 0} emissions · {cadence.executed ?? 0} executed
            · {post.dry_run_passed_count ?? 0} cleared dry-run
          </div>
        </div>
        <span
          className={`rounded-full px-3 py-1 text-xs font-semibold ${
            ready
              ? "bg-emerald-950/60 text-emerald-300 border border-emerald-700/40"
              : "bg-rose-950/60 text-rose-300 border border-rose-700/40"
          }`}
        >
          {ready ? "READY" : "BLOCKED"}
        </span>
      </div>

      {/* Prerequisite checks */}
      <div>
        <div className="text-xs font-mono uppercase tracking-wider text-zinc-500 mb-1">
          Prerequisite gates
        </div>
        {Object.entries(checks).map(([name, check]) => (
          <CheckRow key={name} name={name} check={check} />
        ))}
      </div>

      {/* This is THE answer to "why aren't trades flowing" */}
      <div>
        <div className="text-xs font-mono uppercase tracking-wider text-zinc-500 mb-1">
          What killed intents AFTER dry-run passed
          <span className="ml-2 normal-case text-zinc-600">
            ({post.dry_run_passed_count ?? 0} candidates · {post.executed_count ?? 0} executed
            · {post.missing_auto_submit_row ?? 0} missing audit row)
          </span>
        </div>
        {skipCatEntries.length === 0 ? (
          <p className="text-xs text-zinc-500 italic">
            No post-dry-run skip data in window — either nothing passed dry-run, or
            everything that passed executed.
          </p>
        ) : (
          <div className="space-y-1">
            {skipCatEntries.map(([cat, count]) => (
              <div
                key={cat}
                data-testid={`lane-readiness-skip-${lane}-${cat}`}
                className="flex items-baseline justify-between rounded border border-rose-700/30 bg-rose-950/20 px-2 py-1.5"
              >
                <span className="font-mono text-xs text-rose-200">{cat}</span>
                <span className="font-mono text-sm font-semibold text-rose-100">
                  {count}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Sample intents */}
      {samples.length > 0 && (
        <div>
          <div className="text-xs font-mono uppercase tracking-wider text-zinc-500 mb-1">
            Sample post-dry-run intents
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-zinc-500 border-b border-zinc-800">
                  <th className="py-1 pr-2">Brain</th>
                  <th className="pr-2">Sym</th>
                  <th className="pr-2">Side</th>
                  <th className="pr-2">Conf</th>
                  <th className="pr-2">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {samples.map((s, i) => (
                  <tr
                    key={`${s.intent_id || i}-${i}`}
                    className="border-b border-zinc-900"
                  >
                    <td className="py-1 pr-2 text-zinc-300">{s.stack}</td>
                    <td className="pr-2 font-mono text-amber-400">{s.symbol}</td>
                    <td className="pr-2 text-emerald-400">{s.action}</td>
                    <td className="pr-2 font-mono text-zinc-300">
                      {s.confidence?.toFixed(2) ?? "—"}
                    </td>
                    <td className="pr-2 text-rose-300">
                      {s.skip_category || s.outcome}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Dry-run blocker counts (informational) */}
      {topBlocks.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
            Top dry-run gate failures ({topBlocks.length})
          </summary>
          <div className="mt-2 space-y-1">
            {topBlocks.map((b) => (
              <div
                key={b.gate}
                className="flex items-baseline justify-between border-b border-zinc-900 py-1"
              >
                <span className="font-mono text-zinc-300">{b.gate}</span>
                <span className="font-mono text-zinc-400">{b.fail_count}</span>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export default function LaneReadinessTile() {
  const [data, setData] = useState({ equity: null, crypto: null });
  const [errs, setErrs] = useState({ equity: null, crypto: null });
  const [loading, setLoading] = useState(true);

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
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div
      data-testid="lane-readiness-tile"
      className="rounded-lg border border-slate-700 bg-slate-900/50 p-4 space-y-3"
    >
      <div className="flex items-baseline justify-between">
        <div>
          <h3 className="text-base font-semibold text-slate-100">
            Why isn’t this lane trading?
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            Per-lane readiness check. The “What killed intents AFTER dry-run”
            block names the exact gate. Polls every {POLL_MS / 1000}s.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          data-testid="lane-readiness-refresh"
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
        >
          <ArrowsClockwise className="inline w-3 h-3 mr-1" /> refresh
        </button>
      </div>

      {loading ? (
        <p className="text-xs text-slate-500">loading…</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
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
    </div>
  );
}
