import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning } from "@phosphor-icons/react";

/** Operator knob for AUTO_ROUTER conviction floor. Weak-conviction
 *  intents are sized at floor×base instead of dying SIZED_TO_ZERO.
 *  0 disables the floor. Persisted in runtime_flags, beats env. */
export default function ConvictionFloorKnob() {
  const [state, setState] = useState(null);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/auto-router/conviction-floor");
      setState(data);
      setDraft((d) => (d === null ? data.floor : d));
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || String(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setBusy(true);
    try {
      await api.post("/admin/auto-router/conviction-floor", { value: draft });
      await load();
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!state) return null;
  const dirty = draft !== null && Math.abs(draft - state.floor) > 1e-9;
  const disabled = draft === 0;

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="conviction-floor-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
          Conviction Floor · doctrine dampens, never kills
        </div>
        <span
          className="text-[10px] font-mono font-bold uppercase tracking-widest"
          style={{ color: disabled ? "#EF4444" : "#10B981" }}
          data-testid="conviction-floor-state"
        >
          {disabled ? "FLOOR OFF · HARD KILL" : `FLOOR ×${Number(draft).toFixed(2)}`}
        </span>
      </div>
      <div className="flex items-center gap-3">
        <input
          type="range"
          min="0" max="1" step="0.05"
          value={draft ?? 0}
          onChange={(e) => setDraft(parseFloat(e.target.value))}
          className="flex-1 accent-emerald-500"
          data-testid="conviction-floor-slider"
        />
        <span className="text-xs font-mono font-bold w-10 text-right" data-testid="conviction-floor-value">
          {Number(draft ?? 0).toFixed(2)}
        </span>
        <button
          onClick={save}
          disabled={busy || !dirty}
          data-testid="conviction-floor-save"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? "saving…" : "apply"}
        </button>
      </div>
      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Weak seat×arbiter conviction sizes orders at floor×base instead of SIZED_TO_ZERO.
        Set 0 to restore hard kills. Active: ×{Number(state.floor).toFixed(2)} ({state.source === "operator_knob" ? `set by ${state.updated_by || "operator"}` : "env default"}).
        Takes effect within ~15s.
      </div>
      {err && (
        <div className="mt-2 border border-rd-danger px-2 py-1 text-[10px] font-mono text-rd-danger" data-testid="conviction-floor-error">
          <Warning size={10} className="inline mr-1" />{err}
        </div>
      )}
    </div>
  );
}
