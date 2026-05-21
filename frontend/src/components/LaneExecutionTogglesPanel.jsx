import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui-bits";
import { ShieldCheck, Power, ArrowsClockwise } from "@phosphor-icons/react";

/**
 * LaneExecutionTogglesPanel — operator-owned kill switches per lane.
 *
 * Doctrine pin (2026-02-18):
 *   • equity / crypto toggles are independent
 *   • default OFF (execution opt-in)
 *   • decoupled from broker credential state
 *   • enforced by the `lane_execution_enabled` gate
 *
 * Confirmation gate: flipping a toggle ON requires the operator to
 * confirm — the UI demands a click-through so accidental enables are
 * caught. Flipping OFF is single-click (kill switches should be fast).
 */
export default function LaneExecutionTogglesPanel() {
  const [state, setState] = useState(null);
  const [err, setErr] = useState("");
  const [busyLane, setBusyLane] = useState(null);
  const [confirming, setConfirming] = useState(null); // {lane, enabled}

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/execution/lane-toggles");
      setState(data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const flip = async (lane, enabled) => {
    setBusyLane(lane);
    try {
      const { data } = await api.post("/admin/execution/lane-toggles", { lane, enabled });
      setState((prev) => ({ ...(prev || {}), ...(data?.state || {}) }));
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusyLane(null);
      setConfirming(null);
    }
  };

  const onClick = (lane, currentlyEnabled) => {
    if (currentlyEnabled) {
      // OFF is single-click — kill switch should be fast.
      flip(lane, false);
    } else {
      // ON requires a click-through.
      setConfirming({ lane, enabled: true });
    }
  };

  if (!state) {
    return (
      <Card testid="lane-toggles-loading">
        <div className="text-rd-dim font-mono text-xs">Loading lane toggles…</div>
      </Card>
    );
  }

  return (
    <Card testid="lane-toggles-panel">
      <div className="flex items-center gap-3 mb-3">
        <ShieldCheck size={16} weight="bold" className="text-rd-dim" />
        <span className="label-eyebrow">Lane Execution Toggles</span>
        <button
          onClick={load}
          className="ml-auto p-1 border border-rd-border text-rd-dim hover:text-rd-text"
          data-testid="lane-toggles-reload"
          title="Reload"
        >
          <ArrowsClockwise size={11} weight="bold" />
        </button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {["equity", "crypto"].map((lane) => {
          const enabled = !!state[lane];
          const updatedAt = state[`${lane}_updated_at`];
          const updatedBy = state[`${lane}_updated_by`];
          return (
            <div
              key={lane}
              className="border border-rd-border p-3 flex flex-col gap-2"
              data-testid={`lane-toggle-${lane}`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Power
                    size={14}
                    weight="bold"
                    style={{ color: enabled ? "#10B981" : "#DC2626" }}
                  />
                  <span className="font-mono uppercase text-xs tracking-widest">{lane}</span>
                </div>
                <button
                  onClick={() => onClick(lane, enabled)}
                  disabled={busyLane === lane}
                  data-testid={`lane-toggle-${lane}-btn`}
                  className={
                    enabled
                      ? "px-3 py-1 border text-xs font-mono uppercase tracking-wider border-[#10B981] text-[#10B981] hover:bg-[#10B981]/10 disabled:opacity-50"
                      : "px-3 py-1 border text-xs font-mono uppercase tracking-wider border-rd-danger text-rd-danger hover:bg-rd-danger/10 disabled:opacity-50"
                  }
                >
                  {busyLane === lane
                    ? "…"
                    : enabled
                      ? "ON · click to disable"
                      : "OFF · click to enable"}
                </button>
              </div>
              {updatedAt && (
                <div className="text-[10px] font-mono text-rd-muted">
                  last flip: {new Date(updatedAt).toLocaleString()} by {updatedBy || "?"}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {state.doctrine_note && (
        <div className="text-[10px] font-mono text-rd-muted mt-3 italic leading-relaxed">
          {state.doctrine_note}
        </div>
      )}
      {err && (
        <div className="text-xs font-mono text-rd-danger mt-2" data-testid="lane-toggles-error">
          {err}
        </div>
      )}

      {confirming && (
        <div
          className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4"
          data-testid="lane-toggle-confirm-modal"
        >
          <div className="bg-rd-bg border border-rd-border p-6 max-w-md w-full">
            <div className="label-eyebrow mb-2">Confirm enable</div>
            <div className="font-display text-xl font-bold mb-3 tracking-tight">
              Enable <span className="uppercase text-[#10B981]">{confirming.lane}</span> execution?
            </div>
            <div className="text-xs font-mono text-rd-dim leading-relaxed mb-4">
              This allows MC to route orders on the {confirming.lane} lane through
              its connected broker. Intents that pass the full gate chain will
              be submitted to the broker for real fills (or paper fills for
              Alpaca). This action is audit-logged.
            </div>
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setConfirming(null)}
                className="px-3 py-1 border border-rd-border text-rd-dim hover:text-rd-text text-xs font-mono uppercase tracking-wider"
                data-testid="lane-toggle-confirm-cancel"
              >
                Cancel
              </button>
              <button
                onClick={() => flip(confirming.lane, true)}
                disabled={busyLane === confirming.lane}
                className="px-3 py-1 border border-[#10B981] text-[#10B981] hover:bg-[#10B981]/10 text-xs font-mono uppercase tracking-wider disabled:opacity-50"
                data-testid="lane-toggle-confirm-enable"
              >
                {busyLane === confirming.lane ? "Enabling…" : "Confirm enable"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
