import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning, ArrowsClockwise } from "@phosphor-icons/react";

/**
 * SeatRegistryDriftBanner — surfaces any roster ↔ legacy executor_seat
 * registry mismatch detected by GET /api/admin/seat-registry/diagnose.
 *
 * Renders nothing if there is no drift AND every lane reports
 * would_route_pass=true. When drift OR a vacant execute-seat is
 * detected, renders a small banner at the top of the Intents page so
 * the operator catches the desync before days of executor_seat_check
 * blocks pile up.
 *
 * Read-only. Never mutates. Polls every 30s.
 */
export default function SeatRegistryDriftBanner() {
  const [state, setState] = useState(null);
  const [err, setErr] = useState("");

  const load = async () => {
    try {
      const res = await api.get("/admin/seat-registry/diagnose");
      setState(res.data);
      setErr("");
    } catch (e) {
      // Diagnostic endpoint missing on older deploys — fail silent.
      setErr(e?.response?.data?.detail || e.message);
      setState(null);
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  if (!state || err) return null;

  const drift = Array.isArray(state.drift) ? state.drift : [];
  const lanes = state.lane_executor_summary || {};
  const vacantLanes = Object.entries(lanes).filter(
    ([_, v]) => v && v.would_route_pass === false,
  );

  if (drift.length === 0 && vacantLanes.length === 0) return null;

  return (
    <div
      className="border border-rd-danger bg-rd-danger/5 px-4 py-3 mb-4 font-mono text-[11px] space-y-2"
      data-testid="seat-registry-drift-banner"
    >
      <div className="flex items-center gap-2 text-rd-danger">
        <Warning size={14} weight="bold" />
        <span className="font-bold uppercase tracking-widest">
          Seat registry drift detected
        </span>
        <button
          onClick={load}
          className="ml-auto text-rd-dim hover:text-rd-text"
          title="Re-fetch diagnose"
          data-testid="seat-registry-drift-refresh"
        >
          <ArrowsClockwise size={12} weight="bold" />
        </button>
      </div>

      {drift.map((d) => (
        <div
          key={d.seat}
          className="border-l-2 border-rd-danger pl-3 py-1"
          data-testid={`drift-row-${d.seat}`}
        >
          <div className="text-rd-text">
            seat <span className="font-bold">{d.seat}</span> —
            roster says <span className="text-rd-text">{String(d.roster_says || "vacant")}</span>,
            legacy doc says <span className="text-rd-text">{String(d.legacy_says || "vacant")}</span>,
            gate sees <span className="text-rd-text">{String(d.gate_sees || "vacant")}</span>
          </div>
          <div className="text-rd-dim text-[10px] mt-0.5">{d.fix}</div>
        </div>
      ))}

      {vacantLanes.map(([lane, v]) => (
        <div
          key={lane}
          className="border-l-2 border-rd-warning pl-3 py-1"
          data-testid={`vacant-lane-${lane}`}
        >
          <div className="text-rd-text">
            lane <span className="font-bold uppercase">{lane}</span> — no executor assigned
          </div>
          <div className="text-rd-dim text-[10px] mt-0.5">{v.reason}</div>
        </div>
      ))}

      <div className="text-rd-dim text-[10px] pt-1 border-t border-rd-border">
        Source of truth: Quick Seat Switches. Click a brain pill on the Seats panel to assign.
      </div>
    </div>
  );
}
