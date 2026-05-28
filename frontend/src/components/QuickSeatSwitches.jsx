/**
 * QuickSeatSwitches — one-click seat assignment for all 4 seats per lane.
 *
 * Built in pass #17 (2026-05-27) per operator request:
 *   "Make them simple switches without the required explanation.
 *    Make the explanation optional."
 *
 * Each row = one seat. Click a brain pill = assign. Click ↺ = vacate.
 * Optional reason field at the bottom of each lane (not per-click).
 *
 * Doctrine:
 *   - The eligibility table still enforces who CAN hold which seat
 *     (governor + crypto_governor are Chevelle/RedEye exclusive).
 *     Disallowed cells render as disabled pills.
 *   - Same-lane multi-seating is forbidden (one brain, one role per
 *     lane). The backend handles that — clicking a brain into seat B
 *     vacates seat A automatically.
 *   - Cross-lane multi-seating is fine — same brain can hold
 *     equity-strategist AND crypto-strategist simultaneously.
 *   - Uses POST /api/admin/roster/assign (the generic endpoint), NOT
 *     the per-seat dedicated endpoints like /executor/rotate. The
 *     latter were single-seat tiles with required-reason workflows;
 *     this surface is the lighter one-click variant.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, RUNTIME_META } from "@/lib/api";
import { ArrowsClockwise, X, CircleNotch } from "@phosphor-icons/react";

const BRAINS = ["alpha", "camaro", "chevelle", "redeye"];

// Post-merge 4-seat doctrine (2026-05-27). Auditor = opponent + auditor.
const SEATS_EQUITY = [
  { key: "strategist", label: "STRATEGIST" },
  { key: "governor",   label: "GOVERNOR" },
  { key: "executor",   label: "EXECUTOR" },
  { key: "auditor",    label: "AUDITOR" },
];
const SEATS_CRYPTO = [
  { key: "crypto_strategist", label: "STRATEGIST" },
  { key: "crypto_governor",   label: "GOVERNOR" },
  { key: "crypto",            label: "EXECUTOR" },
  { key: "crypto_auditor",    label: "AUDITOR" },
];

function SeatRow({ seat, holder, eligibility, onAssign, onVacate, busy }) {
  return (
    <div
      className="flex items-center gap-2 py-1.5 border-b border-rd-border/40 last:border-b-0"
      data-testid={`seat-switch-row-${seat.key}`}
    >
      <div
        className="text-[10px] font-mono uppercase tracking-[0.18em] text-rd-dim w-20 shrink-0"
      >
        {seat.label}
      </div>
      <div className="flex items-center gap-1 flex-wrap">
        {BRAINS.map((brain) => {
          const isActive = holder === brain;
          const allowed = eligibility?.[brain]?.[seat.key] !== false;
          const meta = RUNTIME_META[brain] || { color: "#6B7280", label: brain.toUpperCase() };
          return (
            <button
              key={brain}
              onClick={() => !isActive && allowed && !busy && onAssign(seat.key, brain)}
              disabled={busy || !allowed || isActive}
              title={
                !allowed
                  ? `${brain} is not eligible for ${seat.label.toLowerCase()}`
                  : isActive
                  ? `${brain} is currently holding ${seat.label.toLowerCase()}`
                  : `Assign ${brain} to ${seat.label.toLowerCase()}`
              }
              className={
                "px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border transition-colors " +
                (isActive
                  ? "cursor-default"
                  : allowed
                  ? "opacity-60 hover:opacity-100 cursor-pointer"
                  : "opacity-25 cursor-not-allowed line-through")
              }
              style={{
                borderColor: isActive ? meta.color : "#3F3F46",
                color: isActive ? meta.color : "#A1A1AA",
                background: isActive ? `${meta.color}15` : "transparent",
              }}
              data-testid={`seat-switch-${seat.key}-${brain}`}
            >
              <span
                className="inline-block w-1.5 h-1.5 rounded-full mr-1 align-middle"
                style={{ background: meta.color, opacity: isActive ? 1 : 0.5 }}
              />
              {meta.label}
            </button>
          );
        })}
        {holder && (
          <button
            onClick={() => !busy && onVacate(seat.key)}
            disabled={busy}
            title={`Vacate the ${seat.label.toLowerCase()} seat`}
            className="px-1.5 py-0.5 text-[10px] font-mono text-rd-dim border border-rd-border hover:text-rd-warn hover:border-rd-warn"
            data-testid={`seat-switch-${seat.key}-vacate`}
          >
            <X size={10} weight="bold" />
          </button>
        )}
      </div>
    </div>
  );
}

function LaneBlock({ title, seats, assignments, eligibility, onAssign, onVacate, busy, reason, onReasonChange }) {
  return (
    <div className="space-y-1" data-testid={`seat-switches-lane-${title.toLowerCase()}`}>
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-rd-text">
          {title}
        </div>
        <input
          type="text"
          placeholder="optional reason for next assignment…"
          value={reason}
          onChange={(e) => onReasonChange(e.target.value)}
          maxLength={500}
          className="flex-1 max-w-md text-[10px] font-mono bg-rd-bg border border-rd-border px-2 py-0.5 text-rd-dim placeholder:text-rd-dim/50 focus:outline-none focus:border-rd-text/50 focus:text-rd-text"
          data-testid={`seat-switches-reason-${title.toLowerCase()}`}
        />
      </div>
      <div className="border border-rd-border bg-rd-bg/50 px-2">
        {seats.map((s) => (
          <SeatRow
            key={s.key}
            seat={s}
            holder={assignments?.[s.key] || null}
            eligibility={eligibility}
            onAssign={onAssign}
            onVacate={onVacate}
            busy={busy}
          />
        ))}
      </div>
    </div>
  );
}

export default function QuickSeatSwitches() {
  const [roster, setRoster] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [reasonEquity, setReasonEquity] = useState("");
  const [reasonCrypto, setReasonCrypto] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await api.get("/admin/roster");
      setRoster(r.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const eligibility = useMemo(() => roster?.eligibility || {}, [roster]);
  const assignments = useMemo(() => roster?.assignments || {}, [roster]);

  const callAssign = useCallback(async (role, brain) => {
    setBusy(true);
    setErr("");
    try {
      const isCrypto = role === "crypto" || role.startsWith("crypto_");
      const body = { role, brain };
      const reason = (isCrypto ? reasonCrypto : reasonEquity).trim();
      if (reason) body.note = reason;
      await api.post("/admin/roster/assign", body);
      // Refresh state after each assignment.
      await load();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  }, [reasonEquity, reasonCrypto, load]);

  const onAssign = useCallback((role, brain) => callAssign(role, brain), [callAssign]);
  const onVacate = useCallback((role) => callAssign(role, null), [callAssign]);

  if (err && !roster) {
    return (
      <div className="border border-rd-warn bg-rd-bg p-3 text-[11px] font-mono text-rd-warn" data-testid="seat-switches-error">
        seat switches unavailable: {err}
      </div>
    );
  }
  if (!roster) {
    return (
      <div className="border border-rd-border bg-rd-bg p-3 flex items-center gap-2 text-[11px] font-mono text-rd-dim" data-testid="seat-switches-loading">
        <CircleNotch size={12} className="animate-spin" />
        loading seat switches…
      </div>
    );
  }

  return (
    <div className="border border-rd-border bg-rd-bg p-3 space-y-3" data-testid="seat-switches">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <div className="flex items-baseline gap-3 flex-wrap">
          <div className="text-[11px] font-mono uppercase tracking-[0.25em] text-rd-text">
            Quick Seat Switches
          </div>
          <div className="text-[10px] font-mono text-rd-dim">
            click a brain pill to assign · ✕ to vacate · reason is optional
          </div>
        </div>
        <button
          onClick={load}
          disabled={busy}
          className="text-[10px] font-mono text-rd-dim hover:text-rd-text border border-rd-border px-2 py-0.5"
          data-testid="seat-switches-reload"
        >
          <ArrowsClockwise size={10} weight="bold" className="inline mr-1" />
          reload
        </button>
      </div>

      {err && (
        <div className="text-[10px] font-mono text-rd-warn" data-testid="seat-switches-inline-error">
          {err}
        </div>
      )}

      <LaneBlock
        title="Equity Lane"
        seats={SEATS_EQUITY}
        assignments={assignments}
        eligibility={eligibility}
        onAssign={onAssign}
        onVacate={onVacate}
        busy={busy}
        reason={reasonEquity}
        onReasonChange={setReasonEquity}
      />
      <LaneBlock
        title="Crypto Lane"
        seats={SEATS_CRYPTO}
        assignments={assignments}
        eligibility={eligibility}
        onAssign={onAssign}
        onVacate={onVacate}
        busy={busy}
        reason={reasonCrypto}
        onReasonChange={setReasonCrypto}
      />

      <div className="text-[9px] font-mono text-rd-dim leading-relaxed border-t border-rd-border pt-2">
        Eligibility-locked: GOVERNOR seats only accept Chevelle or RedEye
        (operator-pinned doctrine). Disallowed brains render struck-through.
        Same-lane multi-seating auto-vacates the brain's previous seat.
        Cross-lane multi-seating allowed.
      </div>
    </div>
  );
}
