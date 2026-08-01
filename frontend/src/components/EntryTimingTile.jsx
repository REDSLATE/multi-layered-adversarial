import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Timer } from "@phosphor-icons/react";

const Stat = ({ label, value, suffix = "", tone = "text-rd-text", testid }) => (
  <div className="border border-rd-border px-3 py-1.5" data-testid={testid}>
    <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">{label}</div>
    <div className={`font-display text-lg font-bold leading-none ${tone}`}>
      {value ?? "—"}{value != null ? suffix : ""}
    </div>
  </div>
);

/** Entry Timing tile — is chase protection saving the account or
 *  just suppressing opportunity? */
export const EntryTimingTile = () => {
  const [win, setWin] = useState("24h");
  const [data, setData] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.get("/admin/universe/entry-timing/stats")
        .then(({ data: d }) => alive && setData(d.windows))
        .catch(() => {});
    load();
    const t = setInterval(load, 60000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const w = data?.[win];
  return (
    <div className="border border-rd-border bg-rd-panel p-3" data-testid="entry-timing-tile">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Timer size={14} weight="bold" className="text-amber-500" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Entry Timing
          </span>
        </div>
        <div className="flex gap-1">
          {["24h", "7d"].map((k) => (
            <button
              key={k}
              onClick={() => setWin(k)}
              className={`px-2 py-0.5 text-[10px] font-mono uppercase border transition-colors ${
                win === k ? "border-rd-text text-rd-text" : "border-rd-border text-rd-dim hover:text-rd-muted"
              }`}
              data-testid={`entry-timing-window-${k}`}
            >
              {k}
            </button>
          ))}
        </div>
      </div>
      {!w ? (
        <div className="text-[10px] font-mono text-rd-dim">loading…</div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2 mb-2">
            <Stat label="fired" value={w.entries_fired} testid="et-fired" />
            <Stat label="blocked late" value={w.late_entries_blocked} tone="text-amber-500" testid="et-blocked" />
            <Stat label="re-armed" value={w.triggers?.rearmed} tone="text-rd-success" testid="et-rearmed" />
            <Stat label="re-arm filled" value={w.rearmed_filled} tone="text-rd-success" testid="et-rearm-filled" />
            <Stat label="ran w/o pullback" value={w.triggers?.expired} testid="et-expired" />
            <Stat label="broke down" value={w.triggers?.invalidated} tone="text-red-500" testid="et-invalidated" />
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-[10px] font-mono text-rd-muted">
            <span>avg extension at fill: <b className="text-rd-text">{w.avg_extension_at_fill_pct ?? "—"}%</b></span>
            <span>re-entry improvement: <b className="text-rd-success">{w.avg_reentry_improvement_pct ?? "—"}%</b></span>
            <span>chase avoided: <b className="text-rd-success">{w.chase_avoided_avg_pct ?? "—"}%</b></span>
            <span>missed by waiting: <b className="text-amber-500">{w.missed_by_waiting_avg_pct ?? "—"}%</b></span>
          </div>
          <div className="mt-1.5 text-[9px] font-mono text-rd-dim">
            watching now: {w.triggers?.watching ?? 0} · blocked→failed vs blocked→ran vs blocked→re-entered decides the caps
          </div>
        </>
      )}
    </div>
  );
};

export default EntryTimingTile;
