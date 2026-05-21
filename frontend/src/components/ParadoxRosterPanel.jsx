import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { ArrowClockwise, ShieldCheck, Warning } from "@phosphor-icons/react";

/**
 * ParadoxRosterPanel — anchored role × runtime × health matrix.
 *
 * Replaces the old eligibility-matrix editor. Under the PARADOX
 * hierarchy each role is anchored to one runtime forever — the
 * operator cannot swap them at runtime. What CAN change is the
 * health verdict for the anchored seat (e.g. executor seat goes
 * vacant when survival conditions fail).
 *
 * Auditor is intentionally absent — it is the emergent
 * paradox_record artifact, not a seat.
 */

const ROLE_META = {
  strategist: { label: "STRATEGIST", desc: "Forms the directional reasoning. Owns the brain-side decision.",        color: "#3B82F6" },
  executor:   { label: "EXECUTOR",   desc: "Routes orders. Sole writer of broker activity.",                       color: "#F59E0B" },
  governor:   { label: "GOVERNOR",   desc: "Modulates size. May dampen, never blocks.",                            color: "#10B981" },
  opponent:   { label: "OPPONENT",   desc: "Argues the contrary case. Paradox-record co-signer.",                  color: "#DC2626" },
  memory:     { label: "MEMORY",     desc: "Learning core. Namespace-reserved (Shelly not yet running).",          color: "#A855F7" },
};

const RUNTIME_META = {
  alpha:    { label: "ALPHA",    color: "#3B82F6" },
  camaro:   { label: "CAMARO",   color: "#F59E0B" },
  chevelle: { label: "CHEVELLE", color: "#10B981" },
  redeye:   { label: "REDEYE",   color: "#DC2626" },
  shelly:   { label: "SHELLY",   color: "#A855F7" },
};

const SEAT_STATUS_META = {
  occupied: { label: "OCCUPIED", color: "#22C55E" },
  vacant:   { label: "VACANT",   color: "#EF4444" },
  unknown:  { label: "UNKNOWN",  color: "#A1A1AA" },
};

function fmtSeconds(s) {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

export default function ParadoxRosterPanel() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const r = await api.get("/admin/paradox/roster");
      setData(r.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [autoRefresh, refresh]);

  return (
    <Card data-testid="paradox-roster-panel">
      <div className="flex items-center justify-between px-3 py-2 border-b border-rd-border">
        <div>
          <div className="label-eyebrow">PARADOX · roster</div>
          <div className="font-display text-base font-black tracking-tight">
            role × runtime × health
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs font-mono">
          <label className="flex items-center gap-1 text-rd-muted">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              data-testid="paradox-roster-auto-refresh"
            />
            auto-refresh
          </label>
          <button
            onClick={refresh}
            disabled={loading}
            className="border border-rd-border px-2 py-1 text-rd-muted hover:text-rd-text disabled:opacity-40"
            data-testid="paradox-roster-refresh"
            title="Refresh"
          >
            <ArrowClockwise size={12} weight="bold" />
          </button>
        </div>
      </div>

      {err && (
        <div className="px-3 py-2 text-xs font-mono text-rd-danger border-b border-rd-border">
          {err}
        </div>
      )}

      {data && (
        <>
          <div className="px-3 py-2 text-[11px] font-mono text-rd-dim border-b border-rd-border">
            kernel: <span className="text-rd-text font-bold">{data.kernel}</span>
            <span className="mx-2">·</span>
            auditor doctrine: <span className="text-rd-text">{data.auditor_doctrine}</span>
          </div>

          <div className="divide-y divide-rd-border">
            {data.rows.map((r) => (
              <RosterRow key={r.role} row={r} />
            ))}
          </div>
        </>
      )}
    </Card>
  );
}

function RosterRow({ row }) {
  const rm = ROLE_META[row.role] || { label: row.role.toUpperCase(), desc: "", color: "#A1A1AA" };
  const rtm = RUNTIME_META[row.runtime] || { label: (row.runtime || "?").toUpperCase(), color: "#A1A1AA" };
  const ssm = SEAT_STATUS_META[row.seat_status] || SEAT_STATUS_META.unknown;
  const details = row.details || {};
  const failedReasons = details.failed_reasons || [];
  const conditions = details.conditions || {};
  const opponentMode = details.mode; // only set for opponent role

  return (
    <div
      className="px-3 py-3 space-y-2"
      data-testid={`paradox-roster-row-${row.role}`}
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span
            className="font-mono text-xs font-bold tracking-widest"
            style={{ color: rm.color }}
          >
            {rm.label}
          </span>
          <span className="text-rd-dim">→</span>
          <span
            className="font-mono text-xs font-bold tracking-widest"
            style={{ color: rtm.color }}
          >
            {rtm.label}
          </span>
        </div>
        <div className="flex items-center gap-2 text-xs font-mono">
          {row.healthy ? (
            <ShieldCheck size={12} weight="bold" style={{ color: "#22C55E" }} />
          ) : (
            <Warning size={12} weight="bold" style={{ color: "#EF4444" }} />
          )}
          <Badge color={ssm.color}>{ssm.label}</Badge>
          {opponentMode && (
            <Badge color="#06B6D4">
              {opponentMode.replace("_", " ").toUpperCase()}
            </Badge>
          )}
        </div>
      </div>

      <div className="text-[11px] font-mono text-rd-muted">{rm.desc}</div>

      {/* Conditions readout (executor has the most; others may be empty) */}
      {Object.keys(conditions).length > 0 && (
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px] font-mono pt-1">
          {"checkin_age_s" in conditions && (
            <div className="flex justify-between border border-rd-border px-2 py-1">
              <span className="text-rd-dim uppercase tracking-widest">CHECKIN AGE</span>
              <span className={conditions.checkin_fresh ? "text-rd-text" : "text-rd-danger"}>
                {fmtSeconds(conditions.checkin_age_s)}
              </span>
            </div>
          )}
          {"checkin_hash_match" in conditions && (
            <div className="flex justify-between border border-rd-border px-2 py-1">
              <span className="text-rd-dim uppercase tracking-widest">HASH MATCH</span>
              <span className={conditions.checkin_hash_match ? "text-rd-text" : "text-rd-danger"}>
                {conditions.checkin_hash_match ? "yes" : "no"}
              </span>
            </div>
          )}
          {"recent_orphans_24h" in conditions && (
            <div className="flex justify-between border border-rd-border px-2 py-1">
              <span className="text-rd-dim uppercase tracking-widest">ORPHANS 24h</span>
              <span className={conditions.recent_orphans_24h === 0 ? "text-rd-text" : "text-rd-danger"}>
                {conditions.recent_orphans_24h}
              </span>
            </div>
          )}
          {"watchdog_armed" in conditions && (
            <div className="flex justify-between border border-rd-border px-2 py-1">
              <span className="text-rd-dim uppercase tracking-widest">WATCHDOG</span>
              <span className={conditions.watchdog_armed ? "text-rd-text" : "text-rd-danger"}>
                {conditions.watchdog_armed ? "armed" : "disarmed"}
              </span>
            </div>
          )}
        </div>
      )}

      {failedReasons.length > 0 && (
        <div
          className="text-[10px] font-mono text-rd-danger pt-1 space-y-0.5"
          data-testid={`paradox-roster-failed-${row.role}`}
        >
          {failedReasons.map((reason, i) => (
            <div key={i}>✕ {reason}</div>
          ))}
        </div>
      )}

      {details.audit_implication && (
        <div className="text-[10px] font-mono text-rd-muted pt-1">
          → {details.audit_implication}
        </div>
      )}
    </div>
  );
}
