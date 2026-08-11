import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

/** Always-visible per-lane promotion-gate progress — no panel digging. */
export const GateProgressBar = () => {
  const [gate, setGate] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/entry-mode/promotion-gate");
      setGate(data);
    } catch {
      /* silent — strip simply hides */
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [load]);

  if (!gate?.per_lane) return null;
  const minN = Number(gate?.config?.min_n || 30);

  return (
    <div className="border border-rd-border bg-rd-panel px-3 py-2 mb-4 flex flex-wrap items-center gap-x-6 gap-y-2" data-testid="gate-progress-bar">
      <span className="text-[10px] font-mono font-bold uppercase tracking-widest text-rd-text">
        Promotion Gate
      </span>
      {Object.entries(gate.per_lane).map(([lane, v]) => {
        const n = Number(v?.n || 0);
        const pct = Math.min(100, Math.round((n / minN) * 100));
        const crits = (v?.criteria || []).filter((c) => c.name !== "observations");
        return (
          <div key={lane} className="flex items-center gap-2" data-testid={`gate-progress-${lane}`}>
            <span className="text-[10px] font-mono uppercase text-rd-muted w-12">{lane}</span>
            <div className="w-28 h-2 bg-rd-bg border border-rd-border overflow-hidden">
              <div
                className={`h-full transition-[width] duration-700 ${v?.passed ? "bg-emerald-500" : n >= minN ? "bg-amber-500" : "bg-rd-dim"}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="text-[10px] font-mono text-rd-text">{n}/{minN}</span>
            <span className="flex gap-1" title={crits.map((c) => `${c.name}: ${c.value ?? "—"} (${c.threshold})`).join("\n")}>
              {crits.map((c) => (
                <span
                  key={c.name}
                  className={`inline-block w-1.5 h-1.5 rounded-full ${c.pass ? "bg-emerald-500" : "bg-red-500"}`}
                  data-testid={`gate-crit-${lane}-${c.name}`}
                />
              ))}
            </span>
            <span className={`text-[9px] font-mono uppercase tracking-wider ${v?.passed ? "text-emerald-500" : "text-rd-dim"}`}>
              {v?.passed ? "PASS" : n >= minN ? "criteria unmet" : "collecting"}
            </span>
          </div>
        );
      })}
    </div>
  );
};

export default GateProgressBar;
