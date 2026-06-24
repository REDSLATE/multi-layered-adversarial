/**
 * AdvisorPerformanceTile — operator-pinned table (2026-06-24).
 *
 * Answers: "Who is the best advisor, and who is merely noisy?"
 *
 * Columns: brain · appearances · agree% · disagree% · win-rate when
 * agreed · win-rate when disagreed · "right to disagree" %.
 *
 * Doctrine pin: this is OBSERVATION. No mechanic changes drawn from
 * this data until several market days of outcomes accumulate.
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, UsersThree } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 90_000;
const WINDOWS = [24, 72, 168];


function Pct({ value }) {
  if (value === null || value === undefined) {
    return <span className="text-rd-dim">—</span>;
  }
  return <span>{(value * 100).toFixed(1)}%</span>;
}


export default function AdvisorPerformanceTile() {
  const [hours, setHours] = useState(168);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (h) => {
    setLoading(true);
    try {
      const r = await api.get(`/admin/advisor-performance?hours=${h}`);
      setData(r.data);
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(hours);
    const id = setInterval(() => load(hours), POLL_MS);
    return () => clearInterval(id);
  }, [load, hours]);

  const advisors = data?.advisors || [];

  return (
    <div
      className="border-2 border-rd-accent/40 bg-rd-bg2 p-2.5 space-y-2 mt-4"
      data-testid="advisor-performance-tile"
    >
      <div className="flex items-center gap-2">
        <UsersThree size={13} weight="bold" className="text-rd-accent" />
        <div className="flex-1">
          <div className="font-mono text-[11px] uppercase tracking-widest text-rd-text font-bold">
            Advisor Performance · Who&apos;s helping, who&apos;s just talking
          </div>
          <div className="font-mono text-[9px] text-rd-dim mt-0.5">
            Per-advisor agree/disagree rates + executor win-rate when each advisor took a side
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {WINDOWS.map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={
                "px-2 py-0.5 font-mono text-[10px] uppercase border " +
                (hours === h
                  ? "border-rd-accent text-rd-accent"
                  : "border-rd-border text-rd-dim hover:text-rd-text")
              }
              data-testid={`advisor-perf-window-${h}h`}
            >
              {h >= 168 ? `${h / 24}d` : `${h}h`}
            </button>
          ))}
          <button
            onClick={() => load(hours)}
            disabled={loading}
            className="p-1 border border-rd-border text-rd-dim hover:text-rd-text"
            data-testid="advisor-perf-reload"
          >
            <ArrowsClockwise size={11} weight="bold" className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger">
          {String(err)}
        </div>
      )}

      {data && (
        <>
          <div className="font-mono text-[9px] text-rd-dim">
            {data.n_executor_evaluations} executor evaluations ·
            {" "}{data.n_resolved_outcomes} resolved outcomes ·
            window {hours >= 168 ? `${hours / 24}d` : `${hours}h`}
          </div>

          {advisors.length === 0 ? (
            <div className="border border-rd-border p-2 font-mono text-[10px] text-rd-dim">
              No advisor activity in the window yet. The table will populate
              once executor evaluations start logging agree/disagree counts.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mono text-[10px]" data-testid="advisor-perf-table">
                <thead>
                  <tr className="text-rd-dim uppercase border-b border-rd-border">
                    <th className="text-left py-1 pr-2">Advisor</th>
                    <th className="text-right py-1 pr-2">Appearances</th>
                    <th className="text-right py-1 pr-2">Agree %</th>
                    <th className="text-right py-1 pr-2">Disagree %</th>
                    <th className="text-right py-1 pr-2 border-l border-rd-border pl-2">
                      Win rate · agreed
                    </th>
                    <th className="text-right py-1 pr-2">Win rate · disagreed</th>
                    <th className="text-right py-1">Right to disagree</th>
                  </tr>
                </thead>
                <tbody>
                  {advisors.map((a) => (
                    <tr
                      key={a.brain_id}
                      className="border-b border-rd-border/30"
                      data-testid={`advisor-row-${a.brain_id}`}
                    >
                      <td className="py-1 pr-2 text-rd-text font-bold uppercase">{a.brain_id}</td>
                      <td className="py-1 pr-2 text-right text-rd-text">{a.appearances}</td>
                      <td className="py-1 pr-2 text-right text-rd-text">
                        <Pct value={a.agree_pct} />
                      </td>
                      <td className="py-1 pr-2 text-right text-rd-text">
                        <Pct value={a.disagree_pct} />
                      </td>
                      <td className="py-1 pr-2 text-right border-l border-rd-border pl-2">
                        <span
                          className={
                            a.agree_win_rate !== null && a.agree_win_rate >= 0.55
                              ? "text-rd-success"
                              : a.agree_win_rate !== null && a.agree_win_rate < 0.45
                                ? "text-rd-warn"
                                : "text-rd-text"
                          }
                        >
                          <Pct value={a.agree_win_rate} />
                          <span className="text-rd-dim text-[9px] ml-1">
                            ({a.agree_resolved})
                          </span>
                        </span>
                      </td>
                      <td className="py-1 pr-2 text-right text-rd-text">
                        <Pct value={a.disagree_win_rate} />
                        <span className="text-rd-dim text-[9px] ml-1">
                          ({a.disagree_resolved})
                        </span>
                      </td>
                      <td className="py-1 text-right">
                        <span
                          className={
                            a.disagree_was_right_pct !== null && a.disagree_was_right_pct >= 0.55
                              ? "text-rd-success"
                              : "text-rd-text"
                          }
                        >
                          <Pct value={a.disagree_was_right_pct} />
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="font-mono text-[9px] text-rd-dim opacity-70 pt-1 border-t border-rd-border">
            &ldquo;Right to disagree&rdquo; = % of executor LOSSES among trades where this advisor disagreed.
            High = advisor saw something the executor missed.
            Operator doctrine: observe-only. No per-brain weights until several market days
            of resolved outcomes accumulate.
          </div>
        </>
      )}
    </div>
  );
}
