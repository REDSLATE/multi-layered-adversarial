import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import {
  ArrowsLeftRight, ArrowClockwise, UsersThree, ClockCounterClockwise, ToggleLeft, ToggleRight,
} from "@phosphor-icons/react";
import { toast } from "sonner";

// ─── Role metadata. Equity lane = legacy 5 seats; crypto lane = isolated
// council added 2026-02-15 for cross-lane multi-seating. The labels are
// shortened in the crypto column ("EXECUTOR", "GOVERNOR", ...) because the
// lane header already says "CRYPTO" — keeps the cards readable.
const ROLE_META = {
  // ── EQUITY LANE ──
  decider:          { label: "DECIDER",   desc: "Forms the trust / reduce / veto / observation call",                        color: "#F59E0B" },
  executor:         { label: "EXECUTOR",  desc: "Calls the long/short direction. Routes Alpaca orders.",                     color: "#3B82F6" },
  governor:         { label: "GOVERNOR",  desc: "Audits, gates, freezes — never decides, never executes",                    color: "#10B981" },
  advisor:          { label: "ADVISOR",   desc: "Neutral counsel. Off-ladder. Never decides, never executes",                color: "#22C55E" },
  opponent:         { label: "OPPONENT",  desc: "Argues the contrary case. Off-ladder.",                                     color: "#DC2626" },
  // ── CRYPTO LANE ──
  crypto:           { label: "EXECUTOR",  desc: "Crypto execution chair. Routes Kraken orders. $30/order cap.",              color: "#A855F7" },
  crypto_governor:  { label: "GOVERNOR",  desc: "Crypto-only governor. Audits, gates, freezes crypto intents.",              color: "#10B981" },
  crypto_advisor:   { label: "ADVISOR",   desc: "Crypto-only neutral counsel. Off-ladder.",                                  color: "#22C55E" },
  crypto_opponent:  { label: "OPPONENT",  desc: "Crypto-only contrary voice. Off-ladder.",                                   color: "#DC2626" },
};

const BRAIN_META = {
  alpha:    { label: "ALPHA",    color: "#3B82F6" },
  camaro:   { label: "CAMARO",   color: "#F59E0B" },
  chevelle: { label: "CHEVELLE", color: "#10B981" },
  redeye:   { label: "REDEYE",   color: "#DC2626" },
};

const EQUITY_ROLES = ["decider", "executor", "governor", "advisor", "opponent"];
const CRYPTO_ROLES = ["crypto", "crypto_governor", "crypto_advisor", "crypto_opponent"];
const ALL_ROLES = [...EQUITY_ROLES, ...CRYPTO_ROLES];
const BRAINS = ["alpha", "camaro", "chevelle", "redeye"];

const LANE_META = {
  equity: { label: "EQUITY LANE",  desc: "Alpaca · consensus-first governance",      color: "#3B82F6" },
  crypto: { label: "CRYPTO LANE",  desc: "Kraken · momentum-biased, lane-isolated",  color: "#A855F7" },
};

const CHURN_COLOR = { LOW: "#22C55E", MEDIUM: "#F59E0B", HIGH: "#DC2626" };

// Roles that belong to the crypto lane — used for cross-lane multi-seating
// logic in the picker (one brain can hold one equity seat AND one crypto
// seat simultaneously; only within-lane double-seating is auto-vacated).
const _CRYPTO_LANE_SET = new Set(CRYPTO_ROLES);
const _laneOf = (role) => (_CRYPTO_LANE_SET.has(role) ? "crypto" : "equity");

/**
 * Brain Roster panel — operator swaps which brain occupies which role,
 * gated by an eligibility switch matrix. Renders two lanes side-by-side
 * (EQUITY | CRYPTO). Crypto seats are isolated: a brain can hold one
 * seat per lane simultaneously (Camaro: equity decider + crypto opponent,
 * Chevelle: equity governor + crypto governor, etc.).
 *
 * Doctrine: descriptive metadata only. Does not grant execution.
 */
export default function RosterPanel() {
  const [data, setData] = useState(null);
  const [tenure, setTenure] = useState(null);
  const [showEligibility, setShowEligibility] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [r1, r2] = await Promise.all([
        api.get("/admin/roster"),
        api.get("/admin/roster/tenure"),
      ]);
      setData(r1.data);
      setTenure(r2.data);
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    const id = setInterval(refresh, 30000);
    return () => clearInterval(id);
  }, [refresh]);

  const action = async (label, fn) => {
    setBusy(true);
    try {
      await fn();
      toast.success(label);
      await refresh();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  if (!data) return null;
  const assignments = data.assignments || {};
  const eligibility = data.eligibility || {};
  const tenureByRole = Object.fromEntries((tenure?.per_role || []).map(r => [r.role, r]));

  const renderSlot = (role) => (
    <RoleSlot
      key={role}
      role={role}
      occupant={assignments[role]}
      assignments={assignments}
      eligibility={eligibility}
      tenure={tenureByRole[role]}
      onAssign={(brain) =>
        action(
          `${BRAIN_META[brain]?.label || brain.toUpperCase()} → ${ROLE_META[role].label} (${_laneOf(role).toUpperCase()})`,
          () => api.post("/admin/roster/assign", { role, brain }),
        )
      }
      onVacate={() =>
        action(
          `${ROLE_META[role].label} (${_laneOf(role).toUpperCase()}) vacated`,
          () => api.post("/admin/roster/assign", { role, brain: null }),
        )
      }
      busy={busy}
    />
  );

  return (
    <Card className="p-0 overflow-hidden mb-6" testid="roster-panel">
      <div className="px-4 py-3 border-b border-rd-border flex items-baseline justify-between gap-3 flex-wrap">
        <div className="flex items-baseline gap-3">
          <UsersThree size={14} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">Brain roster</span>
          <Badge color="#A1A1AA">DESCRIPTIVE METADATA</Badge>
          {tenure && (
            <Badge color={CHURN_COLOR[tenure.churn_state] || "#A1A1AA"}>
              {tenure.churn_state} CHURN
            </Badge>
          )}
        </div>
        <div className="flex items-baseline gap-3">
          <button
            type="button"
            onClick={() => setShowEligibility(!showEligibility)}
            className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-text flex items-center gap-1 font-mono"
            data-testid="roster-eligibility-toggle"
          >
            {showEligibility ? <ToggleRight size={11} weight="bold" /> : <ToggleLeft size={11} weight="bold" />}
            eligibility switches
          </button>
          <button
            type="button"
            onClick={() => action("Reset to doctrine defaults", () => api.post("/admin/roster/reset"))}
            disabled={busy}
            className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-text flex items-center gap-1 font-mono"
            data-testid="roster-reset-btn"
          >
            <ArrowClockwise size={10} weight="bold" /> reset
          </button>
        </div>
      </div>

      {/* Two-lane layout: equity lane (5 seats) on the left, crypto lane
          (4 seats) on the right. On narrow screens they stack. */}
      <div className="grid grid-cols-1 lg:grid-cols-9 divide-y lg:divide-y-0 lg:divide-x divide-rd-border">
        <LaneHeader lane="equity" />
        <div className="lg:col-span-4 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 divide-y sm:divide-y-0 sm:divide-x divide-rd-border" data-testid="roster-lane-equity">
          {EQUITY_ROLES.map(renderSlot)}
        </div>
        <LaneHeader lane="crypto" />
        <div className="lg:col-span-3 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 divide-y sm:divide-y-0 sm:divide-x divide-rd-border" data-testid="roster-lane-crypto">
          {CRYPTO_ROLES.map(renderSlot)}
        </div>
      </div>

      {showEligibility && (
        <EligibilityMatrix
          eligibility={eligibility}
          assignments={assignments}
          onToggle={(brain, role, allowed) =>
            action(
              `${BRAIN_META[brain]?.label} · ${ROLE_META[role].label} (${_laneOf(role).toUpperCase()}) ${allowed ? "ALLOWED" : "BLOCKED"}`,
              () => api.post("/admin/roster/eligibility", { brain, role, allowed }),
            )
          }
          busy={busy}
        />
      )}

      {tenure && (
        <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border text-[10px] font-mono text-rd-dim flex items-baseline justify-between flex-wrap gap-2">
          <div className="flex items-baseline gap-3">
            <ClockCounterClockwise size={10} weight="bold" />
            <span>avg tenure: <span className="text-rd-text">{tenure.average_tenure_display}</span></span>
            <span>swaps 90d: <span className="text-rd-text">{tenure.total_swaps_90d}</span></span>
            {tenure.last_swap && (
              <span>
                last swap: <span className="text-rd-text">{tenure.last_swap.action}</span>
                {" · "}
                {formatAge(tenure.last_swap.age_days)} ago
              </span>
            )}
          </div>
          <span className="uppercase tracking-widest">{tenure.doctrine_invariant}</span>
        </div>
      )}

      <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border text-[10px] text-rd-dim uppercase tracking-widest leading-relaxed">
        {data.doctrine}
      </div>
    </Card>
  );
}

function LaneHeader({ lane }) {
  const meta = LANE_META[lane];
  return (
    <div
      className="lg:col-span-1 px-3 py-3 bg-rd-bg2 flex lg:flex-col items-baseline lg:items-start justify-between lg:justify-start gap-2 lg:gap-3 border-b lg:border-b-0 border-rd-border"
      data-testid={`roster-lane-header-${lane}`}
    >
      <Badge color={meta.color}>{meta.label}</Badge>
      <div className="text-[10px] text-rd-dim font-mono leading-relaxed lg:writing-mode-horizontal">
        {meta.desc}
      </div>
    </div>
  );
}

function RoleSlot({ role, occupant, assignments, eligibility, tenure, onAssign, onVacate, busy }) {
  const [picking, setPicking] = useState(false);
  const meta = ROLE_META[role];
  const myLane = _laneOf(role);

  // Reverse lookup: which roles does each brain currently hold? A brain
  // may hold one equity seat AND one crypto seat simultaneously, so we
  // need to know per-lane whether they're already booked.
  const lanesOfBrain = {};
  for (const [r, b] of Object.entries(assignments)) {
    if (!b) continue;
    if (!lanesOfBrain[b]) lanesOfBrain[b] = {};
    lanesOfBrain[b][_laneOf(r)] = r;
  }

  return (
    <div className="px-4 py-3 min-h-[140px]" data-testid={`roster-slot-${role}`}>
      <div className="flex items-baseline justify-between gap-2 mb-1.5">
        <Badge color={meta.color}>{meta.label}</Badge>
        {tenure?.tenure_display && tenure.tenure_display !== "—" && (
          <span className="text-[10px] font-mono text-rd-dim" data-testid={`roster-tenure-${role}`}>
            in role: <span className="text-rd-text">{tenure.tenure_display}</span>
          </span>
        )}
      </div>
      <div className="text-[10px] text-rd-dim leading-relaxed font-mono mb-3">
        {meta.desc}
      </div>

      {occupant ? (
        <div className="flex items-baseline gap-2 mb-2 flex-wrap" data-testid={`roster-occupant-${role}`}>
          <Badge color={BRAIN_META[occupant]?.color}>
            {BRAIN_META[occupant]?.label || occupant.toUpperCase()}
          </Badge>
          {tenure?.previous_role && (
            <span className="text-[10px] font-mono text-rd-muted">
              prev · {ROLE_META[tenure.previous_role]?.label?.toLowerCase()}
            </span>
          )}
        </div>
      ) : (
        <div className="text-[11px] font-mono text-rd-dim italic mb-2">
          — vacant —
        </div>
      )}

      {!picking ? (
        <div className="flex items-baseline gap-2">
          <button
            type="button"
            onClick={() => setPicking(true)}
            disabled={busy}
            className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-text flex items-center gap-1 font-mono"
            data-testid={`roster-save-btn-${role}`}
          >
            <ArrowsLeftRight size={10} weight="bold" /> save
          </button>
          {occupant && (
            <button
              type="button"
              onClick={onVacate}
              disabled={busy}
              className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-danger font-mono"
              data-testid={`roster-vacate-btn-${role}`}
            >
              vacate
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-1" data-testid={`roster-picker-${role}`}>
          {BRAINS.map((brain) => {
            // Within-lane multi-seating is auto-vacated by the backend.
            // Cross-lane multi-seating is allowed and labeled, not blocked.
            const sameLaneRole = lanesOfBrain[brain]?.[myLane];
            const elsewhereSameLane = sameLaneRole && sameLaneRole !== role ? sameLaneRole : null;
            const otherLane = myLane === "equity" ? "crypto" : "equity";
            const otherLaneRole = lanesOfBrain[brain]?.[otherLane];
            const isCurrent = occupant === brain;
            const isEligible = !!eligibility?.[brain]?.[role];
            return (
              <button
                key={brain}
                type="button"
                onClick={() => {
                  setPicking(false);
                  if (!isCurrent && isEligible) onAssign(brain);
                }}
                disabled={busy || isCurrent || !isEligible}
                className={`w-full text-left text-[11px] font-mono flex items-baseline gap-2 px-2 py-1 border ${
                  isCurrent
                    ? "border-rd-text bg-rd-bg3 text-rd-text"
                    : isEligible
                      ? "border-rd-border hover:bg-rd-bg2 text-rd-text"
                      : "border-rd-border text-rd-dim opacity-50 cursor-not-allowed"
                }`}
                data-testid={`roster-pick-${role}-${brain}`}
                title={isEligible ? "click to save this brain into this position" : `${brain} is not eligible for ${role} — toggle in the switches matrix`}
              >
                <Badge color={BRAIN_META[brain].color}>{BRAIN_META[brain].label}</Badge>
                {!isEligible && (
                  <span className="text-[10px] text-rd-danger ml-auto">BLOCKED</span>
                )}
                {isEligible && elsewhereSameLane && (
                  <span className="text-[10px] text-rd-warning ml-auto">
                    will vacate {ROLE_META[elsewhereSameLane].label.toLowerCase()}
                  </span>
                )}
                {isEligible && !elsewhereSameLane && otherLaneRole && (
                  <span className="text-[10px] text-rd-dim ml-auto">
                    also holds {ROLE_META[otherLaneRole].label.toLowerCase()} ({otherLane})
                  </span>
                )}
                {isCurrent && (
                  <span className="text-[10px] text-rd-dim ml-auto">in this role</span>
                )}
              </button>
            );
          })}
          <button
            type="button"
            onClick={() => setPicking(false)}
            className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-text font-mono mt-1"
          >
            cancel
          </button>
        </div>
      )}
    </div>
  );
}

function EligibilityMatrix({ eligibility, assignments, onToggle, busy }) {
  return (
    <div className="px-4 py-3 bg-rd-bg2 border-t border-rd-border" data-testid="eligibility-matrix">
      <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
        Eligibility switches · which brains may hold which seats
      </div>
      <div className="overflow-x-auto">
        <table className="text-[11px] font-mono w-full">
          <thead>
            <tr className="text-rd-dim uppercase tracking-widest text-[10px]">
              <th className="text-left py-1.5 pr-3 align-bottom" rowSpan={2}>brain \ role</th>
              <th className="text-center py-1 px-2 border-b border-rd-border" colSpan={EQUITY_ROLES.length}>
                <Badge color={LANE_META.equity.color}>EQUITY</Badge>
              </th>
              <th className="text-center py-1 px-2 border-b border-rd-border" colSpan={CRYPTO_ROLES.length}>
                <Badge color={LANE_META.crypto.color}>CRYPTO</Badge>
              </th>
            </tr>
            <tr className="text-rd-dim uppercase tracking-widest text-[10px]">
              {ALL_ROLES.map((r) => (
                <th key={r} className="text-center py-1.5 px-2">
                  {ROLE_META[r].label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {BRAINS.map((b) => (
              <tr key={b} className="border-t border-rd-border">
                <td className="py-1.5 pr-3">
                  <Badge color={BRAIN_META[b].color}>{BRAIN_META[b].label}</Badge>
                </td>
                {ALL_ROLES.map((r) => {
                  const allowed = !!eligibility?.[b]?.[r];
                  const isCurrent = assignments?.[r] === b;
                  return (
                    <td key={r} className="text-center py-1.5 px-2">
                      <button
                        type="button"
                        onClick={() => !busy && onToggle(b, r, !allowed)}
                        disabled={busy || (isCurrent && allowed)}
                        title={isCurrent ? "currently in this role — vacate or swap first to disable" : ""}
                        className={`inline-flex items-center justify-center w-12 h-6 border ${
                          allowed
                            ? "border-rd-success bg-rd-success/10 text-rd-success"
                            : "border-rd-border bg-rd-bg3 text-rd-dim"
                        } ${isCurrent && allowed ? "opacity-50 cursor-not-allowed" : "hover:brightness-125"}`}
                        data-testid={`eligibility-cell-${b}-${r}`}
                      >
                        {allowed ? "ALLOW" : "BLOCK"}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-2 text-[10px] text-rd-dim leading-relaxed">
        Toggling a switch off is blocked while the brain currently holds that seat (vacate or swap first).
        Eligibility is descriptive — it constrains future role assignments, not execution.
      </div>
    </div>
  );
}

function formatAge(days) {
  if (days == null) return "—";
  if (days < (1 / 24)) return `${Math.max(Math.round(days * 24 * 60), 1)}m`;
  if (days < 1) return `${Math.round(days * 24)}h`;
  if (days < 30) return `${Math.round(days)}d`;
  return `${Math.round(days / 30)}mo`;
}
