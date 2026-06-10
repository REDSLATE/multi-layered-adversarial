import React, { useEffect, useRef, useState, useCallback } from "react";
import { subscribeMcStream } from "@/hooks/useMcStream";
import { RUNTIME_META } from "@/lib/api";

/**
 * Ephemeral position-misread toast host (P1, 2026-06-10).
 *
 * Operator needs to SEE — not query — every position misread the instant
 * it lands. Mounted once at the layout level so a misread on ANY admin
 * page draws the eye. Each toast lives for ~5s then fades.
 *
 * Anatomy of the message:
 *   "Camaro just misread AAPL — assumed FLAT, broker says SHORT"
 *
 * Stacking rules:
 *   * Newest on top.
 *   * Max 4 visible — older ones drop silently (the PositionMisreadsCard
 *     is the durable record; toasts are a fire-alarm only).
 *   * Mouse-over pauses the auto-dismiss timer so an operator can read
 *     it through without it vanishing mid-glance.
 *   * One toast per (detected_at + symbol + brain) — re-renders or
 *     stream replays will NOT spam duplicate toasts.
 *
 * Doctrine pin: This is the LAST defense against the 2026-06-09 AAPL
 * incident. Brain misreads broker truth → broker fills the wrong way →
 * money lost. The misread row exists. The PositionMisreadsCard exists.
 * But neither helps if the operator was on a different page when it
 * happened. This toast does.
 */

const SIDE_COLOR = {
  long:  "#10B981",
  short: "#EF4444",
  flat:  "#71717A",
};

const TOAST_TTL_MS = 5_000;
const MAX_VISIBLE = 4;

function MisreadPill({ side }) {
  const color = SIDE_COLOR[(side || "").toLowerCase()] || "#52525B";
  return (
    <span
      className="inline-block px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-widest"
      style={{
        background: `${color}22`,
        color,
        border: `1px solid ${color}`,
      }}
    >
      {side || "—"}
    </span>
  );
}

function Toast({ toast, onDismiss }) {
  const [paused, setPaused] = useState(false);
  const [hidden, setHidden] = useState(false);

  // Fade-out 200ms before actual removal so the user perceives motion.
  useEffect(() => {
    if (paused) return;
    const fadeAt = Math.max(TOAST_TTL_MS - 200, 0);
    const fadeTimer = setTimeout(() => setHidden(true), fadeAt);
    const dropTimer = setTimeout(() => onDismiss(toast.id), TOAST_TTL_MS);
    return () => {
      clearTimeout(fadeTimer);
      clearTimeout(dropTimer);
    };
  }, [paused, toast.id, onDismiss]);

  const meta = RUNTIME_META[(toast.brain || "").toLowerCase()];
  // Operator-facing brand (Camino / Barracuda / Hellcat / GTO) — never
  // leak the internal slot code (alpha/camaro/chevelle/redeye) to the
  // dashboard. Fall back to a capitalized raw value if an unknown
  // brain ever appears so the toast still says something useful.
  const brainDisplay = meta?.roleTitle
    || ((toast.brain || "").charAt(0).toUpperCase() + (toast.brain || "").slice(1))
    || "Brain";
  const brainColor = meta?.color || "#EF4444";

  return (
    <div
      role="alert"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      data-testid={`misread-toast-${toast.symbol}`}
      className="pointer-events-auto bg-rd-bg2 border-l-4 shadow-2xl px-4 py-3 mb-2 transition-opacity duration-200"
      style={{
        borderColor: "#EF4444",
        boxShadow: "0 8px 32px -4px rgba(239, 68, 68, 0.35)",
        opacity: hidden ? 0 : 1,
        minWidth: 340,
        maxWidth: 480,
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-[10px] uppercase tracking-widest text-rose-400 font-bold mb-1">
            ⚠ Position misread
          </div>
          <div className="text-sm text-rd-text leading-snug">
            <span
              className="font-display font-black"
              style={{ color: brainColor }}
              data-testid="misread-toast-brain"
            >
              {brainDisplay}
            </span>
            <span className="text-rd-muted"> just misread </span>
            <span className="font-mono font-bold" data-testid="misread-toast-symbol">
              {toast.symbol}
            </span>
          </div>
          <div className="mt-2 flex items-center gap-2 text-[11px]">
            <span className="text-rd-dim uppercase tracking-wider">assumed</span>
            <MisreadPill side={toast.assumed_side} />
            <span className="text-rd-dim uppercase tracking-wider">· broker says</span>
            <MisreadPill side={toast.actual_side} />
          </div>
          {toast.emitted_action && (
            <div className="mt-1 text-[10px] text-rd-muted font-mono uppercase tracking-widest">
              emitted: {toast.emitted_action}
              {toast.missed_short_profit && (
                <span className="ml-2 text-rose-400">· missed_short</span>
              )}
            </div>
          )}
        </div>
        <button
          onClick={() => onDismiss(toast.id)}
          className="text-rd-dim hover:text-rd-text leading-none text-lg -mr-1 -mt-1"
          aria-label="Dismiss"
          data-testid="misread-toast-dismiss"
        >
          ×
        </button>
      </div>
    </div>
  );
}

export default function MisreadToastHost() {
  const [toasts, setToasts] = useState([]);
  // Track keys we've already shown so a stream replay or React re-render
  // doesn't pop the same toast twice. Ref because we only need
  // identity-tracking, not re-render on change.
  const seenRef = useRef(new Set());

  const handleDismiss = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  // Subscribe imperatively to the shared SSE stream. We only care
  // about `position_misread` events; everything else is a no-op for
  // this host.
  useEffect(() => {
    const unsub = subscribeMcStream({
      onEvent: (name, payload) => {
        if (name !== "position_misread") return;
        const key = `${payload.detected_at}|${payload.symbol}|${payload.brain}`;
        if (seenRef.current.has(key)) return;
        seenRef.current.add(key);
        const fresh = {
          id: key,
          symbol: payload.symbol || "—",
          brain: payload.brain || "",
          assumed_side: payload.assumed_side || "",
          actual_side: payload.actual_side || "",
          emitted_action: payload.emitted_action || "",
          missed_short_profit: !!payload.missed_short_profit,
        };
        setToasts((prev) => [fresh, ...prev].slice(0, MAX_VISIBLE));
      },
    });
    return unsub;
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div
      className="fixed top-4 right-4 z-[60] pointer-events-none flex flex-col items-end"
      data-testid="misread-toast-host"
    >
      {toasts.map((t) => (
        <Toast key={t.id} toast={t} onDismiss={handleDismiss} />
      ))}
    </div>
  );
}
