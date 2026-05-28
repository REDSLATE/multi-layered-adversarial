/**
 * SeatRosterStrip — operator-pinned panel showing all 4 seats + holders
 * + contribution freshness in one view.
 *
 * Why this exists (pass #15, 2026-05-27):
 *   The Intents page was showing "STRATEGIST -0.26 conviction · ADVERSARY
 *   4 objections · GOVERNOR doctrine_reject" on every intent as if four
 *   brain voices had weighed in. Truth: those are computed deterministically
 *   by shared/doctrine/brain_sidecars.py from base labels. The seat
 *   holders (Alpha/RedEye/Chevelle/Camaro) were silent on
 *   shared_brain_opinions while the doctrine fallback kept stamping
 *   identical advisory numbers per lane.
 *
 *   This strip puts the truth front-and-centre:
 *     - Which brain holds each seat (per lane)
 *     - When that brain last posted a fresh opinion
 *     - When that brain last contributed sovereign state
 *     - Heartbeat freshness vs. contribution freshness — the gap
 *       between them is where the deception lived.
 *
 *   Doctrine pin: this strip is READ-ONLY. No seat changes happen
 *   here. It surfaces facts; the operator decides.
 */
import React, { useCallback, useEffect, useState } from "react";
import { api, RUNTIME_META } from "@/lib/api";
import { CircleNotch, WarningCircle, CheckCircle, Pulse } from "@phosphor-icons/react";

const ROLES_EQUITY = [
  { key: "strategist", label: "STRATEGIST", desc: "conviction signal" },
  { key: "opponent",   label: "OPPONENT",   desc: "argues contrary case" },
  { key: "governor",   label: "GOVERNOR",   desc: "risk sizer" },
  { key: "executor",   label: "EXECUTOR",   desc: "fires intents" },
  { key: "auditor",    label: "AUDITOR",    desc: "post-trade reviewer" },
];

const ROLES_CRYPTO = [
  { key: "crypto_strategist", label: "STRATEGIST", desc: "conviction signal" },
  { key: "crypto_opponent",   label: "OPPONENT",   desc: "argues contrary case" },
  { key: "crypto_governor",   label: "GOVERNOR",   desc: "risk sizer" },
  { key: "crypto",            label: "EXECUTOR",   desc: "fires intents" },
  { key: "crypto_auditor",    label: "AUDITOR",    desc: "post-trade reviewer" },
];

// Freshness thresholds (mirror sidecar_diagnostics.py).
const HB_FRESH_SEC = 180;
const SV_FRESH_SEC = 240;
const OPINION_FRESH_SEC = 3600;  // 1h — opinions are slower-cadence than HB

const NEVER_THRESHOLD_SEC = 7 * 24 * 3600;  // 7d → treat as effectively never

function formatAge(ageSec) {
  if (ageSec < 60) return `${Math.floor(ageSec)}s ago`;
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`;
  if (ageSec < 86400) return `${Math.floor(ageSec / 3600)}h ago`;
  return `${Math.floor(ageSec / 86400)}d ago`;
}

function freshnessChip(ageSec, threshold) {
  if (ageSec == null || ageSec < 0 || ageSec > NEVER_THRESHOLD_SEC) {
    return { color: "#DC2626", label: "never", icon: WarningCircle };
  }
  if (ageSec < threshold) {
    return { color: "#10B981", label: formatAge(ageSec), icon: CheckCircle };
  }
  if (ageSec < threshold * 4) {
    return { color: "#F59E0B", label: formatAge(ageSec), icon: Pulse };
  }
  return { color: "#DC2626", label: formatAge(ageSec), icon: WarningCircle };
}

function brainSwatch(brain) {
  const meta = RUNTIME_META[brain] || { color: "#6B7280", label: (brain || "—").toUpperCase() };
  return (
    <span
      className="inline-flex items-center gap-1.5 px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider border"
      style={{ borderColor: meta.color, color: meta.color }}
      data-testid={`seat-roster-brain-${brain}`}
    >
      <span
        className="inline-block w-1.5 h-1.5 rounded-full"
        style={{ background: meta.color }}
      />
      {meta.label}
    </span>
  );
}

function SeatCell({ role, holder, diag }) {
  // diag is the row from /api/admin/sidecar-diagnostics for the holder brain.
  const opinionAge = diag?.opinions?.age_seconds;
  const svAge = diag?.sovereign_contribution?.age_seconds;
  const opinionChip = freshnessChip(opinionAge, OPINION_FRESH_SEC);
  const svChip = freshnessChip(svAge, SV_FRESH_SEC);
  const OpinionIcon = opinionChip.icon;
  const SvIcon = svChip.icon;
  const isEmpty = !holder;

  return (
    <div
      className="flex flex-col gap-1.5 border border-rd-border bg-rd-bg p-2 min-w-0"
      data-testid={`seat-cell-${role.key}`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[10px] font-mono uppercase tracking-[0.18em] text-rd-dim shrink-0">
          {role.label}
        </span>
        {isEmpty ? (
          <span className="text-[10px] font-mono text-rd-dim italic" data-testid={`seat-empty-${role.key}`}>
            unseated
          </span>
        ) : (
          brainSwatch(holder)
        )}
      </div>
      <div className="text-[9px] text-rd-dim leading-tight">{role.desc}</div>
      {!isEmpty && (
        <div className="flex flex-col gap-0.5 mt-0.5">
          <div className="flex items-center gap-1 text-[9px] font-mono">
            <OpinionIcon size={9} weight="bold" style={{ color: opinionChip.color }} />
            <span className="text-rd-dim w-12 shrink-0">opinion</span>
            <span style={{ color: opinionChip.color }}>{opinionChip.label}</span>
          </div>
          <div className="flex items-center gap-1 text-[9px] font-mono">
            <SvIcon size={9} weight="bold" style={{ color: svChip.color }} />
            <span className="text-rd-dim w-12 shrink-0">sovereign</span>
            <span style={{ color: svChip.color }}>{svChip.label}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function LaneRow({ title, roles, assignments, diagByBrain }) {
  return (
    <div className="space-y-1.5" data-testid={`seat-roster-lane-${title.toLowerCase()}`}>
      <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-rd-text">
        {title}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-1.5">
        {roles.map((r) => {
          const holder = assignments?.[r.key] || null;
          const diag = holder ? diagByBrain[holder] : null;
          return <SeatCell key={r.key} role={r} holder={holder} diag={diag} />;
        })}
      </div>
    </div>
  );
}

export default function SeatRosterStrip() {
  const [roster, setRoster] = useState(null);
  const [diag, setDiag] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const [r1, r2] = await Promise.all([
        api.get("/admin/roster"),
        api.get("/admin/sidecar-diagnostics"),
      ]);
      setRoster(r1.data);
      setDiag(r2.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  if (err && !roster) {
    return (
      <div
        className="border border-rd-warn bg-rd-bg p-3 text-[11px] font-mono text-rd-warn"
        data-testid="seat-roster-error"
      >
        seat roster unavailable: {err}
      </div>
    );
  }
  if (!roster || !diag) {
    return (
      <div
        className="border border-rd-border bg-rd-bg p-3 flex items-center gap-2 text-[11px] font-mono text-rd-dim"
        data-testid="seat-roster-loading"
      >
        <CircleNotch size={12} className="animate-spin" />
        loading seat roster…
      </div>
    );
  }

  const diagByBrain = {};
  for (const row of diag.brains || []) {
    diagByBrain[row.brain] = row;
  }

  const assignments = roster.assignments || {};

  // Build a "fleet at a glance" inline summary so the operator gets a
  // single-line read of who's actually contributing.
  const fleet = diag.fleet || {};
  const opinionDeadCount = (diag.brains || []).filter(
    (b) => b.opinions?.age_seconds == null || b.opinions.age_seconds > OPINION_FRESH_SEC * 4,
  ).length;

  return (
    <div
      className="border border-rd-border bg-rd-bg p-3 space-y-3"
      data-testid="seat-roster-strip"
    >
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <div className="flex items-baseline gap-3 flex-wrap">
          <div className="text-[11px] font-mono uppercase tracking-[0.25em] text-rd-text">
            Seat Roster
          </div>
          <div className="text-[10px] font-mono text-rd-dim">
            who's holding each seat · last fresh opinion · last sovereign contribution
          </div>
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono">
          <span className="text-rd-dim">fleet:</span>
          <span style={{ color: "#10B981" }} data-testid="seat-roster-fleet-connected">
            ✓ {fleet.connected_count ?? "—"} connected
          </span>
          <span style={{ color: opinionDeadCount > 0 ? "#DC2626" : "#10B981" }} data-testid="seat-roster-opinion-silent">
            {opinionDeadCount > 0 ? "⚠" : "✓"} {opinionDeadCount} opinion-silent
          </span>
        </div>
      </div>

      <LaneRow
        title="Equity Lane"
        roles={ROLES_EQUITY}
        assignments={assignments}
        diagByBrain={diagByBrain}
      />
      <LaneRow
        title="Crypto Lane"
        roles={ROLES_CRYPTO}
        assignments={assignments}
        diagByBrain={diagByBrain}
      />

      <div className="text-[9px] font-mono text-rd-dim leading-relaxed border-t border-rd-border pt-2">
        Read-only. Authority lives on the seat policy, not on this strip.
        "opinion-silent" means the brain isn't posting to
        <code className="px-1 text-rd-text">/api/opinions</code> — when this
        happens MC falls back to deterministic doctrine sidecars and the
        intent page may show identical "strategist/adversary/governor" values
        across every symbol in a lane.
      </div>
    </div>
  );
}
