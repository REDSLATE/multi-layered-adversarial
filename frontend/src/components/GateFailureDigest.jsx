import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning } from "@phosphor-icons/react";

const WINDOWS = [24, 48, 72];

const STATE_COLORS = {
  blocked: "#EF4444",
  advisory_only: "#F59E0B",
  no_trade: "#71717A",
  expired_unrouted: "#8B5CF6",
};

/** Daily roll-up of which doctrine checks kill the most intents. */
export default function GateFailureDigest() {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async (h) => {
    try {
      const { data: d } = await api.get(`/admin/gate-failure-digest?hours=${h}`);
      setData(d);
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || String(e));
    }
  }, []);

  useEffect(() => { load(hours); }, [hours, load]);

  const max = data?.top_reasons?.[0]?.count || 1;

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="gate-failure-digest">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
          Gate Failure Digest · what kills intents
        </div>
        <div className="flex items-center gap-1">
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => setHours(w)}
              data-testid={`gate-digest-window-${w}`}
              className={`text-[10px] font-mono px-2 py-0.5 border ${hours === w ? "border-rd-text text-rd-text font-bold" : "border-rd-border text-rd-dim hover:border-rd-text"}`}
            >
              {w}H
            </button>
          ))}
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger px-2 py-1 text-[10px] font-mono text-rd-danger" data-testid="gate-digest-error">
          <Warning size={10} className="inline mr-1" />{err}
        </div>
      )}

      {data && !err && (
        <>
          <div className="text-xs font-mono mb-2" data-testid="gate-digest-total">
            <span className="font-bold">{(data.total_killed ?? 0).toLocaleString()}</span>
            <span className="text-rd-dim"> intents killed in {data.hours}h</span>
          </div>

          {(data.top_reasons || []).length === 0 ? (
            <div className="text-xs text-rd-dim font-mono italic p-2 border border-dashed border-rd-border">
              No kills in window — every intent routed or is still pending.
            </div>
          ) : (
            <div className="space-y-1" data-testid="gate-digest-reasons">
              {data.top_reasons.slice(0, 10).map((r) => {
                const color = STATE_COLORS[r.gate_state] || "#71717A";
                return (
                  <div key={`${r.reason}-${r.lane}-${r.gate_state}`} className="flex items-center gap-2 text-[11px] font-mono">
                    <span className="w-12 text-right font-bold" style={{ color }}>{r.count}</span>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-rd-text">{r.reason}</span>
                        <span className="text-[9px] text-rd-dim shrink-0">{r.lane} · {r.gate_state}</span>
                      </div>
                      <div className="h-1 mt-0.5 rounded" style={{ width: `${Math.max(2, (r.count / max) * 100)}%`, backgroundColor: color, opacity: 0.7 }} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {(data.by_day || []).length > 1 && (
            <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2" data-testid="gate-digest-days">
              {data.by_day.slice(0, 3).map((d) => (
                <div key={d.day} className="border border-rd-border/60 p-2">
                  <div className="text-[9px] uppercase tracking-widest text-rd-dim font-mono mb-1">
                    {d.day} · {d.total.toLocaleString()} killed
                  </div>
                  {d.top.map((t) => (
                    <div key={t.reason} className="flex justify-between text-[10px] font-mono">
                      <span className="truncate text-rd-text mr-2">{t.reason}</span>
                      <span className="text-rd-dim">{t.count}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}

          <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
            Tune the checks that dominate: SIZED_TO_ZERO → raise conviction floor ·
            market_closed_preflight → enable extended hours · master_switch_disarmed → arm the switch.
            Max window 72h (retention purge).
          </div>
        </>
      )}
    </div>
  );
}
