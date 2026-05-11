import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { ArrowsLeftRight, ArrowClockwise, UsersThree } from "@phosphor-icons/react";
import { toast } from "sonner";

const ROLE_META = {
  decider:  { label: "DECIDER",  desc: "Forms the trust / reduce / veto / observation call",          color: "#F59E0B" },
  executor: { label: "EXECUTOR", desc: "Would carry orders to broker if execution were enabled",      color: "#3B82F6" },
  governor: { label: "GOVERNOR", desc: "Audits, gates, freezes — never decides, never executes",      color: "#10B981" },
  advisor:  { label: "ADVISOR",  desc: "Whispers context to the decider — never decides, never exec", color: "#DC2626" },
};

const BRAIN_META = {
  alpha:    { label: "ALPHA",    color: "#3B82F6" },
  camaro:   { label: "CAMARO",   color: "#F59E0B" },
  chevelle: { label: "CHEVELLE", color: "#10B981" },
  redeye:   { label: "REDEYE",   color: "#DC2626" },
};

const ROLES = ["decider", "executor", "governor", "advisor"];
const BRAINS = ["alpha", "camaro", "chevelle", "redeye"];

/**
 * Brain Roster panel — operator swaps which brain occupies which role.
 * Doctrine: descriptive metadata only. Does not grant execution.
 */
export default function RosterPanel() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/roster");
      setData(data);
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

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

  return (
    <Card className="p-0 overflow-hidden mb-6" testid="roster-panel">
      <div className="px-4 py-3 border-b border-rd-border flex items-baseline justify-between">
        <div className="flex items-baseline gap-3">
          <UsersThree size={14} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">Brain roster</span>
          <Badge color="#A1A1AA">DESCRIPTIVE METADATA</Badge>
        </div>
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

      <div className="grid grid-cols-1 md:grid-cols-4 divide-y md:divide-y-0 md:divide-x divide-rd-border">
        {ROLES.map((role) => (
          <RoleSlot
            key={role}
            role={role}
            occupant={assignments[role]}
            assignments={assignments}
            onAssign={(brain) =>
              action(
                `${BRAIN_META[brain]?.label || brain.toUpperCase()} → ${ROLE_META[role].label}`,
                () => api.post("/admin/roster/assign", { role, brain }),
              )
            }
            onVacate={() =>
              action(
                `${ROLE_META[role].label} vacated`,
                () => api.post("/admin/roster/assign", { role, brain: null }),
              )
            }
            busy={busy}
          />
        ))}
      </div>

      <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border text-[10px] text-rd-dim uppercase tracking-widest leading-relaxed">
        {data.doctrine}
      </div>
    </Card>
  );
}

function RoleSlot({ role, occupant, assignments, onAssign, onVacate, busy }) {
  const [picking, setPicking] = useState(false);
  const meta = ROLE_META[role];

  // Other roles each brain occupies, so picker can warn the operator.
  const roleOfBrain = {};
  for (const [r, b] of Object.entries(assignments)) {
    if (b) roleOfBrain[b] = r;
  }

  return (
    <div className="px-4 py-3 min-h-[120px]" data-testid={`roster-slot-${role}`}>
      <div className="flex items-baseline gap-2 mb-1.5">
        <Badge color={meta.color}>{meta.label}</Badge>
      </div>
      <div className="text-[10px] text-rd-dim leading-relaxed font-mono mb-3">
        {meta.desc}
      </div>

      {occupant ? (
        <div className="flex items-baseline gap-2 mb-2" data-testid={`roster-occupant-${role}`}>
          <Badge color={BRAIN_META[occupant]?.color}>
            {BRAIN_META[occupant]?.label || occupant.toUpperCase()}
          </Badge>
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
            data-testid={`roster-swap-btn-${role}`}
          >
            <ArrowsLeftRight size={10} weight="bold" /> change
          </button>
          {occupant && (
            <button
              type="button"
              onClick={onVacate}
              disabled={busy}
              className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-danger font-mono"
            >
              vacate
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-1" data-testid={`roster-picker-${role}`}>
          {BRAINS.map((brain) => {
            const elsewhere = roleOfBrain[brain] && roleOfBrain[brain] !== role
              ? roleOfBrain[brain]
              : null;
            const isCurrent = occupant === brain;
            return (
              <button
                key={brain}
                type="button"
                onClick={() => {
                  setPicking(false);
                  if (!isCurrent) onAssign(brain);
                }}
                disabled={busy || isCurrent}
                className={`w-full text-left text-[11px] font-mono flex items-baseline gap-2 px-2 py-1 border ${
                  isCurrent
                    ? "border-rd-text bg-rd-bg3 text-rd-text"
                    : "border-rd-border hover:bg-rd-bg2 text-rd-text"
                }`}
                data-testid={`roster-pick-${role}-${brain}`}
              >
                <Badge color={BRAIN_META[brain].color}>{BRAIN_META[brain].label}</Badge>
                {elsewhere && (
                  <span className="text-[10px] text-rd-warning ml-auto">
                    currently {ROLE_META[elsewhere].label.toLowerCase()}
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
