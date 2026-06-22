import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import {
  Flask, Power, Warning, CheckCircle, XCircle, ArrowsClockwise,
} from "@phosphor-icons/react";

/**
 * LegacyWrapperTogglePanel — operator A/B diagnostic tool
 * (2026-02-19).
 *
 * Per-brain switch for the legacy personality wrappers. Each brain
 * (camino / barracuda / hellcat / gto) has one wrapper applied:
 *   * camino    → alpha_legacy_doctrine
 *   * barracuda → camaro_legacy_doctrine
 *   * hellcat   → chevelle_legacy_doctrine
 *   * gto       → redeye_legacy_doctrine
 *
 * When the operator suspects the penalty-stacking wrappers are
 * compressing `size_bias` toward zero (causing the downstream
 * cap_per_order rejection cascade), this panel lets them switch
 * off ONE wrapper at a time and observe whether 403/502 frequency
 * drops by ~25%.
 *
 * No restart required — toggles take effect on the next intent
 * emit. Disables require a typed reason (audit-trail). All toggles
 * are written to `shared_wrapper_toggle_audit` so the history is
 * recoverable.
 */
const BRAIN_LABELS = {
  // 2026-06-22 — wrappers renamed to drop the seat-title suffix.
  // Display label now mirrors the canonical wrapper-registry key so
  // operators can grep the audit log without translating between the
  // dashboard pill and the Python symbol.
  camino:    { display: "CAMINO",    wrapperShort: "ALPHA · legacy doctrine" },
  barracuda: { display: "BARRACUDA", wrapperShort: "CAMARO · legacy doctrine" },
  hellcat:   { display: "HELLCAT",   wrapperShort: "CHEVELLE · legacy doctrine" },
  gto:       { display: "GTO",       wrapperShort: "REDEYE · legacy doctrine" },
};

function BrainToggleRow({ row, onToggle, busy }) {
  const [showReasonInput, setShowReasonInput] = useState(false);
  const [reason, setReason] = useState("");
  const label = BRAIN_LABELS[row.brain_id] || {
    display: row.brain_id.toUpperCase(),
    wrapperShort: row.wrapper,
  };
  const disabled = row.disabled;
  const reasonValid = reason.trim().length >= 4;

  const handleClick = () => {
    if (disabled) {
      // Re-enable — no reason needed.
      onToggle({ brain_id: row.brain_id, disabled: false, reason: "" });
      return;
    }
    setShowReasonInput(true);
  };

  const confirmDisable = () => {
    if (!reasonValid) return;
    onToggle({ brain_id: row.brain_id, disabled: true, reason: reason.trim() });
    setShowReasonInput(false);
    setReason("");
  };

  return (
    <div
      className={
        "border p-2 space-y-1.5 " +
        (disabled
          ? "border-rd-warn bg-rd-warn/5"
          : "border-rd-border bg-rd-bg")
      }
      data-testid={`wrapper-row-${row.brain_id}`}
    >
      <div className="flex items-center gap-2">
        <div className="flex-1 min-w-0">
          <div className="font-mono text-xs text-rd-text font-bold uppercase tracking-wider">
            {label.display}
          </div>
          <div className="font-mono text-[10px] text-rd-dim truncate">
            {label.wrapperShort}
          </div>
        </div>
        <button
          onClick={handleClick}
          disabled={busy}
          className={
            "px-2 py-1 font-mono text-[10px] uppercase tracking-wider border flex items-center gap-1 " +
            (disabled
              ? "border-rd-warn text-rd-warn hover:bg-rd-warn/10"
              : "border-rd-success text-rd-success hover:bg-rd-success/10")
          }
          data-testid={`wrapper-toggle-${row.brain_id}`}
          title={disabled ? "Re-enable wrapper" : "Disable wrapper"}
        >
          <Power size={10} weight="bold" />
          {disabled ? "Disabled" : "Active"}
        </button>
      </div>
      {disabled && row.reason && (
        <div className="text-[10px] font-mono text-rd-warn flex items-start gap-1">
          <Warning size={10} weight="bold" className="mt-0.5 shrink-0" />
          <span>
            <span className="text-rd-dim">{row.source}:</span> {row.reason}
          </span>
        </div>
      )}
      {showReasonInput && (
        <div className="border-t border-rd-border pt-1.5 space-y-1">
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why disable this wrapper? (≥4 chars, audit-logged)"
            rows={2}
            className="w-full bg-rd-bg2 border border-rd-border px-2 py-1 font-mono text-[10px] text-rd-text focus:outline-none focus:border-rd-warn resize-none"
            data-testid={`wrapper-reason-${row.brain_id}`}
          />
          <div className="flex items-center gap-1 justify-end">
            <button
              onClick={() => { setShowReasonInput(false); setReason(""); }}
              className="px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider border border-rd-border text-rd-dim"
            >
              Cancel
            </button>
            <button
              onClick={confirmDisable}
              disabled={!reasonValid}
              className={
                "px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider border " +
                (reasonValid
                  ? "border-rd-warn text-rd-warn hover:bg-rd-warn/10"
                  : "border-rd-border text-rd-dim cursor-not-allowed")
              }
              data-testid={`wrapper-confirm-disable-${row.brain_id}`}
            >
              Confirm disable
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function LegacyWrapperTogglePanel() {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await api.get("/admin/wrappers/status");
      setStatus(res.data);
      setErr(null);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onToggle = async (payload) => {
    setBusy(true);
    try {
      const res = await api.post("/admin/wrappers/toggle", {
        brain_id: payload.brain_id,
        disabled: payload.disabled,
        reason: payload.reason,
      });
      setStatus(res.data.status);
      toast.success(
        payload.disabled
          ? `Wrapper DISABLED for ${payload.brain_id.toUpperCase()} — observe 403/502 rate`
          : `Wrapper RE-ENABLED for ${payload.brain_id.toUpperCase()}`,
      );
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      toast.error(typeof d === "string" ? d : JSON.stringify(d));
    } finally {
      setBusy(false);
    }
  };

  const rows = status?.wrappers || [];
  const disabledCount = rows.filter((r) => r.disabled).length;

  return (
    <div
      className="border border-rd-border bg-rd-bg2 p-3 space-y-3"
      data-testid="legacy-wrapper-panel"
    >
      <div className="flex items-center gap-2">
        <Flask size={13} weight="bold" className="text-rd-accent" />
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-text">
          Legacy Wrapper A/B Switch
        </span>
        <span className="text-[10px] font-mono text-rd-dim ml-2">
          {disabledCount > 0
            ? `· ${disabledCount}/${rows.length} disabled`
            : `· all ${rows.length} active`}
        </span>
        <button
          onClick={load}
          className="ml-auto p-1 border border-rd-border text-rd-dim hover:text-rd-text"
          title="Refresh"
          data-testid="wrapper-panel-reload"
        >
          <ArrowsClockwise size={10} weight="bold" />
        </button>
      </div>

      <div className="text-[10px] font-mono text-rd-dim leading-relaxed">
        Disable ONE wrapper at a time. Observe 403/502 frequency on the
        Intents feed for ~10 min. If it drops by ~25% per disabled
        wrapper, the penalty-stacking multiplier effect is confirmed.
        Re-enable when done. No restart required.
      </div>

      {err && (
        <div className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5">
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          {err}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {rows.map((row) => (
          <BrainToggleRow
            key={row.brain_id}
            row={row}
            onToggle={onToggle}
            busy={busy}
          />
        ))}
      </div>

      {disabledCount > 0 && (
        <div className="border border-rd-warn bg-rd-warn/5 px-2 py-1 font-mono text-[10px] text-rd-warn flex items-start gap-1.5" data-testid="wrapper-panel-warning">
          <Warning size={11} weight="bold" className="mt-0.5 shrink-0" />
          <span>
            {disabledCount} wrapper(s) are bypassed — affected brains are
            now emitting raw doctrine intents without legacy personality
            dampening. Re-enable when the A/B experiment is complete.
          </span>
        </div>
      )}

      {disabledCount === 0 && status && (
        <div className="font-mono text-[10px] text-rd-dim flex items-center gap-1.5" data-testid="wrapper-panel-ok">
          <CheckCircle size={10} weight="bold" />
          All wrappers active · doctrine matrix at baseline
        </div>
      )}
    </div>
  );
}
