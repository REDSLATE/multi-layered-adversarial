import React, { useEffect, useState } from "react";
import { X, Warning, ShieldCheck } from "@phosphor-icons/react";

/**
 * SubmitOrderModal — operator confirmation for routing an intent to
 * the broker. Replaces the legacy `window.prompt` + `window.confirm`
 * pair (which couldn't carry the BUY/SELL toggle or override reason).
 *
 * Doctrine (2026-02-19):
 *   * Notional defaults to the per-lane cap but operator can dial
 *     down. Never above the cap.
 *   * BUY/SELL toggle defaults to the brain's emitted action.
 *     Operator can flip; the receipt stamps the original action.
 *   * Operator override checkbox lifts every soft gate. Reason
 *     ≥ 8 chars required (backend enforces; UI mirrors).
 *   * Money safety stays: the $1-$10 per-ticker cap + freeze are
 *     enforced regardless of the override flag.
 */
export default function SubmitOrderModal({
  open,
  intent,           // { intent_id, symbol, action, lane, stack, ... }
  capUsd,           // resolved per-order cap for the intent's lane
  onConfirm,        // (payload) => Promise<void>
  onClose,
}) {
  const [notional, setNotional] = useState("");
  const [side, setSide] = useState("BUY");
  const [override, setOverride] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Reset state every time the modal opens for a new intent.
  useEffect(() => {
    if (!open) return;
    setNotional(capUsd != null ? String(capUsd) : "");
    const brainAction = (intent?.action || "BUY").toUpperCase();
    setSide(brainAction === "SELL" ? "SELL" : "BUY");
    setOverride(false);
    setOverrideReason("");
    setSubmitting(false);
  }, [open, intent?.intent_id, capUsd, intent?.action]);

  if (!open || !intent) return null;

  const laneLabel = (intent.lane || "GLOBAL").toUpperCase();
  const notionalNum = Number(notional);
  const notionalValid =
    Number.isFinite(notionalNum) && notionalNum > 0 && notionalNum <= (capUsd ?? Infinity);
  const reasonValid = !override || overrideReason.trim().length >= 8;
  const brainAction = (intent.action || "").toUpperCase();
  const actionChanged = side !== brainAction && (brainAction === "BUY" || brainAction === "SELL");
  const canConfirm = notionalValid && reasonValid && !submitting;

  const handleConfirm = async () => {
    if (!canConfirm) return;
    setSubmitting(true);
    try {
      await onConfirm({
        order_notional_usd: notionalNum,
        action_override:
          // Only send action_override if the operator actually chose a
          // different side from the brain's emit. Keeps the receipt
          // stamp accurate (action_overridden=false when no flip).
          actionChanged || brainAction === "HOLD" ? side : null,
        operator_override: override,
        override_reason: override ? overrideReason.trim() : "",
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      data-testid="submit-modal"
    >
      <div
        className="w-full max-w-md border border-rd-border bg-rd-bg shadow-xl"
        role="dialog"
        aria-modal="true"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-rd-border px-4 py-3">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
              Route to broker · {laneLabel}
            </div>
            <div className="font-mono text-sm text-rd-text mt-0.5">
              {intent.symbol}{" "}
              <span className="text-rd-dim">·</span>{" "}
              <span className="text-rd-dim">{intent.stack?.toUpperCase()}</span>{" "}
              <span className="text-rd-dim">·</span>{" "}
              <span style={{ color: brainAction === "BUY" ? "#10B981" : brainAction === "SELL" ? "#DC2626" : "#A1A1AA" }}>
                emit: {brainAction || "—"}
              </span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 text-rd-dim hover:text-rd-text"
            data-testid="submit-modal-close"
          >
            <X size={16} weight="bold" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {/* Notional */}
          <div>
            <label className="text-[10px] font-mono uppercase tracking-widest text-rd-dim block mb-1.5">
              Notional (USD)
            </label>
            <input
              type="number"
              min="0.01"
              max={capUsd ?? undefined}
              step="0.01"
              value={notional}
              onChange={(e) => setNotional(e.target.value)}
              className="w-full bg-rd-bg2 border border-rd-border px-3 py-2 font-mono text-sm text-rd-text focus:outline-none focus:border-rd-accent"
              data-testid="submit-modal-notional"
              autoFocus
            />
            <div className="text-[10px] font-mono text-rd-dim mt-1">
              {capUsd != null ? (
                <>
                  Per-order cap: <span className="text-rd-accent">${capUsd.toFixed(2)}</span> · lane={laneLabel}
                </>
              ) : (
                <span className="text-rd-danger">cap unavailable</span>
              )}
              {!notionalValid && notional.length > 0 && (
                <span className="text-rd-danger ml-2">
                  · must be 0 &lt; N ≤ ${(capUsd ?? 0).toFixed(2)}
                </span>
              )}
            </div>
          </div>

          {/* Action override */}
          <div>
            <label className="text-[10px] font-mono uppercase tracking-widest text-rd-dim block mb-1.5">
              Action
            </label>
            <div className="grid grid-cols-2 gap-2">
              {["BUY", "SELL"].map((s) => (
                <button
                  key={s}
                  onClick={() => setSide(s)}
                  className={
                    "py-2 font-mono text-xs font-bold uppercase tracking-widest border transition-colors " +
                    (side === s
                      ? s === "BUY"
                        ? "border-rd-success bg-rd-success/10 text-rd-success"
                        : "border-rd-danger bg-rd-danger/10 text-rd-danger"
                      : "border-rd-border bg-rd-bg2 text-rd-dim hover:text-rd-text")
                  }
                  data-testid={`submit-modal-side-${s.toLowerCase()}`}
                >
                  {s}
                </button>
              ))}
            </div>
            {actionChanged && (
              <div className="text-[10px] font-mono text-rd-accent mt-1.5 flex items-start gap-1.5">
                <Warning size={11} weight="bold" className="mt-0.5 shrink-0" />
                Action flipped from brain's emit ({brainAction} → {side}). Receipt will stamp original action.
              </div>
            )}
            {brainAction === "HOLD" && (
              <div className="text-[10px] font-mono text-rd-danger mt-1.5 flex items-start gap-1.5">
                <Warning size={11} weight="bold" className="mt-0.5 shrink-0" />
                Brain emitted HOLD — you must pick a side to route this intent.
              </div>
            )}
          </div>

          {/* Operator override */}
          <div className="border border-rd-border bg-rd-bg2 p-3 space-y-2">
            <label className="flex items-start gap-2 cursor-pointer" data-testid="submit-modal-override-toggle">
              <input
                type="checkbox"
                checked={override}
                onChange={(e) => setOverride(e.target.checked)}
                className="mt-0.5"
              />
              <div className="flex-1">
                <div className="font-mono text-xs text-rd-text">
                  Operator override
                </div>
                <div className="font-mono text-[10px] text-rd-dim leading-relaxed mt-0.5">
                  Bypasses every soft gate (seat check, spread floor, RR ratio,
                  council, universe match, etc). Per-ticker money caps + broker
                  freeze stay enforced.
                </div>
              </div>
            </label>
            {override && (
              <div className="pl-6 space-y-1">
                <textarea
                  value={overrideReason}
                  onChange={(e) => setOverrideReason(e.target.value)}
                  placeholder="Why is this trade bypassing the gate chain? (≥8 chars, audit-logged)"
                  rows={2}
                  className="w-full bg-rd-bg border border-rd-border px-2 py-1.5 font-mono text-[11px] text-rd-text focus:outline-none focus:border-rd-accent resize-none"
                  data-testid="submit-modal-override-reason"
                />
                <div className="text-[10px] font-mono text-rd-dim">
                  {overrideReason.trim().length}/8 min ·{" "}
                  {reasonValid ? (
                    <span className="text-rd-success">OK</span>
                  ) : (
                    <span className="text-rd-danger">reason too short</span>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="border-t border-rd-border px-4 py-3 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 font-mono text-[11px] uppercase tracking-widest border border-rd-border text-rd-dim hover:text-rd-text"
            data-testid="submit-modal-cancel"
          >
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            disabled={!canConfirm}
            className={
              "px-3 py-1.5 font-mono text-[11px] uppercase tracking-widest border flex items-center gap-1.5 transition-colors " +
              (canConfirm
                ? "border-rd-accent bg-rd-accent/10 text-rd-accent hover:bg-rd-accent/20"
                : "border-rd-border bg-rd-bg2 text-rd-dim cursor-not-allowed")
            }
            data-testid="submit-modal-confirm"
          >
            <ShieldCheck size={12} weight="bold" />
            {submitting ? "Routing…" : `Route ${side} $${Number.isFinite(notionalNum) ? notionalNum.toFixed(2) : "?"}`}
          </button>
        </div>
      </div>
    </div>
  );
}
