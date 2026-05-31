import React, { useState } from "react";
import {
  CaretDown, CaretUp, Sparkle, MagnifyingGlass, Shield, Lightning, Warning,
} from "@phosphor-icons/react";

/**
 * DoctrineStrip — full-width row strip rendered beneath an Intent row.
 *
 * Renders the read-only doctrine packet attached to an intent:
 *   • quality band (A_QUALITY / B_QUALITY / C_QUALITY / REJECT)
 *   • setup score
 *   • 4 role chips (strategist · adversary · governor · execution_judge)
 *
 * Doctrine pin (2026-02-17): packets are ADVISORY ONLY. They do not
 * influence execution until the promotion-gate condition fires
 * (min_samples >= 100 + statistical proof). This UI must never imply
 * the packet caused a block/allow — only that it OBSERVED.
 */

const QUALITY_COLOR = {
  A_QUALITY: "#10B981",
  B_QUALITY: "#84CC16",
  C_QUALITY: "#F59E0B",
  REJECT: "#DC2626",
};

const QUALITY_BG = {
  A_QUALITY: "rgba(16,185,129,0.12)",
  B_QUALITY: "rgba(132,204,22,0.12)",
  C_QUALITY: "rgba(245,158,11,0.12)",
  REJECT: "rgba(220,38,38,0.12)",
};

const SEAT_ICON = {
  strategist: Sparkle,
  adversary: MagnifyingGlass,
  governor: Shield,
};

const SEAT_LABEL = {
  strategist: "Strategist",
  // Display-only rename (2026-02-17 prod feedback): the doctrine packet
  // still keys this role as `adversary` for backend schema continuity
  // (5+ collections store adversary-keyed audit + scorecard rows), but
  // the user-facing label here aligns with the canonical AUDITOR seat
  // it merged into per the 8-seat IP. Same brain holds the seat; the
  // word "Adversary" was carrying obsolete combative framing.
  adversary: "Auditor",
  governor: "Governor",
};

// The four-seat strip renders ONLY the real seats now. `execution_judge`
// was a doctrine-output label masquerading as a seat (2026-05-31
// demotion). Its data is still shipped in the packet under
// `seats.execution_judge` (key kept for scorecard backward-compat),
// but it's rendered as an inline Setup-Quality badge under the DOCTRINE
// pill — never as a peer chip — to stop it visually implying authority.
const REAL_SEATS = ["strategist", "adversary", "governor"];

function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(digits);
}

function seatHeadline(role, seat) {
  if (!seat) return { text: "—", color: "#A1A1AA", flagged: false };
  if (role === "strategist") {
    const d = Number(seat.conviction_delta || 0);
    const sign = d > 0 ? "+" : "";
    return {
      text: `${sign}${d.toFixed(2)} conviction`,
      color: d > 0 ? "#10B981" : d < 0 ? "#DC2626" : "#A1A1AA",
      flagged: d < 0,
    };
  }
  if (role === "adversary") {
    const n = Array.isArray(seat.objections) ? seat.objections.length : 0;
    const cs = Number(seat.challenge_strength || 0);
    return {
      text: n > 0 ? `${n} objection${n > 1 ? "s" : ""} · cs ${cs.toFixed(2)}` : "no objections",
      color: n > 0 ? (cs >= 0.6 ? "#DC2626" : "#F59E0B") : "#A1A1AA",
      flagged: n > 0,
    };
  }
  if (role === "governor") {
    const rm = Number(seat.risk_multiplier ?? 1);
    // 2026-05-18 operator patch — distinguish HARD_BLOCK (true safety
    // veto) from RISK_DOWN (silence / soft dissent / low score). The
    // backend now emits `display_status` + `reason`; fall back to the
    // legacy block_reasons + risk_multiplier shape if missing.
    const ds = String(seat.display_status || "").toUpperCase();
    const reason = seat.reason || (seat.block_reasons || [])[0] || null;
    if (ds === "RISK_DOWN" || (ds === "" && (seat.block_reasons || []).length > 0 && rm > 0)) {
      const reasonShort = reason ? ` · ${reason}` : "";
      return {
        text: `RISK_DOWN ×${rm.toFixed(2)}${reasonShort}`,
        color: "#F59E0B",
        flagged: true,
      };
    }
    const isHardBlock = ds === "BLOCK" || (ds === "" && ((seat.block_reasons || []).length > 0 || rm === 0));
    if (isHardBlock) {
      const reasonShort = reason ? ` · ${reason}` : "";
      return {
        text: `BLOCK${reasonShort}`,
        color: "#DC2626",
        flagged: true,
      };
    }
    return {
      text: `modulate ×${rm.toFixed(2)}`,
      color: rm < 0.8 ? "#F59E0B" : "#A1A1AA",
      flagged: rm < 0.8,
    };
  }
  if (role === "execution_judge") {
    // No longer rendered as a peer seat chip (2026-05-31 demotion).
    // Kept here only as a defensive fallback in case any caller still
    // passes the legacy role string.
    return { text: "advisory", color: "#A1A1AA", flagged: false };
  }
  return { text: "—", color: "#A1A1AA", flagged: false };
}

function SeatChip({ role, seat, testid }) {
  const Icon = SEAT_ICON[role] || Warning;
  const head = seatHeadline(role, seat);
  return (
    <span
      className="inline-flex items-center gap-1.5 px-2 py-0.5 border border-rd-border bg-rd-bg"
      data-testid={testid}
      title={seat?.holder ? `holder: ${seat.holder}` : "seat vacant"}
    >
      <Icon size={10} weight="bold" style={{ color: head.color }} />
      <span className="text-[10px] font-mono uppercase tracking-wider text-rd-dim">
        {SEAT_LABEL[role]}
      </span>
      <span className="text-[10px] font-mono text-rd-dim">·</span>
      <span
        className="text-[10px] font-mono"
        style={{ color: head.color, fontWeight: head.flagged ? 600 : 400 }}
      >
        {head.text}
      </span>
      {seat?.holder && (
        <span className="text-[9px] font-mono text-rd-dim opacity-70">
          ({seat.holder})
        </span>
      )}
    </span>
  );
}

function SeatDetailCard({ role, seat }) {
  if (!seat) return null;
  const Icon = SEAT_ICON[role] || Warning;
  const head = seatHeadline(role, seat);
  const list = [];
  if (role === "adversary" && (seat.objections || []).length) {
    list.push(["Objections", seat.objections]);
  }
  if (role === "governor" && (seat.block_reasons || []).length) {
    list.push(["Block Reasons", seat.block_reasons]);
  }
  if (role === "execution_judge" && seat.execution_checks) {
    const failed = Object.entries(seat.execution_checks)
      .filter(([, v]) => v === false || v === null || v === undefined)
      .map(([k]) => k);
    if (failed.length) list.push(["Failed Checks", failed]);
  }
  return (
    <div
      className="border border-rd-border bg-rd-bg2 p-3"
      data-testid={`doctrine-seat-detail-${role}`}
    >
      <div className="flex items-center gap-2 mb-2">
        <Icon size={12} weight="bold" style={{ color: head.color }} />
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-text">
          {SEAT_LABEL[role]}
        </span>
        <span className="text-[10px] font-mono text-rd-dim ml-auto">
          seat: <span className="text-rd-text">{seat.seat || "—"}</span>
          <span className="mx-1">·</span>
          holder: <span className="text-rd-text">{seat.holder || "vacant"}</span>
        </span>
      </div>
      <div className="flex items-baseline gap-2 mb-2">
        <span
          className="font-mono text-xs"
          style={{ color: head.color, fontWeight: 600 }}
        >
          {head.text.toUpperCase()}
        </span>
        {role === "strategist" && (
          <span className="font-mono text-[10px] text-rd-dim">
            Δconv {fmtNum(seat.conviction_delta, 3)}
          </span>
        )}
        {role === "adversary" && (
          <span className="font-mono text-[10px] text-rd-dim">
            challenge_strength {fmtNum(seat.challenge_strength, 3)}
          </span>
        )}
        {role === "governor" && (
          <span className="font-mono text-[10px] text-rd-dim">
            risk_multiplier ×{fmtNum(seat.risk_multiplier, 3)}
          </span>
        )}
      </div>
      {list.map(([label, items]) => (
        <div key={label} className="mb-1.5">
          <div className="text-[10px] uppercase tracking-wider text-rd-dim mb-1">
            {label}
          </div>
          <ul className="space-y-0.5">
            {items.map((it) => (
              <li
                key={it}
                className="text-[11px] font-mono text-rd-text flex items-start gap-1.5"
              >
                <span className="text-rd-danger">›</span>
                <span>{String(it).replaceAll("_", " ")}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {seat.lesson && (
        <div className="text-[10px] font-mono text-rd-dim italic leading-relaxed mt-2 pt-2 border-t border-rd-border">
          {seat.lesson}
        </div>
      )}
    </div>
  );
}

// ───────────────────────── Setup-Quality badge ──────────────────────
// Renders the (demoted, advisory-only) setup-quality summary as a
// small inline badge alongside the DOCTRINE quality pill. NEVER renders
// as a peer seat chip — that's the visual lie this demotion fixes.
function SetupQualityBadge({ seat, testid }) {
  const ok = !!(seat?.summary_ok ?? seat?.execution_ready);
  const failed = (seat?.failed_checks && Array.isArray(seat.failed_checks))
    ? seat.failed_checks
    : Object.entries(seat?.execution_checks || {})
        .filter(([, v]) => v === false || v === null || v === undefined)
        .map(([k]) => k);
  const text = ok
    ? "setup ok"
    : failed.length === 1
      ? `setup: ${failed[0]}`
      : `setup: ${failed.length} checks failed`;
  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-0.5 border font-mono text-[10px] uppercase tracking-wider"
      style={{
        borderColor: ok ? "#10B981" : "#A1A1AA",
        color: ok ? "#10B981" : "#A1A1AA",
        background: "transparent",
      }}
      title="Setup-quality summary · ADVISORY ONLY · does not gate execution"
      data-testid={testid}
    >
      {text}
    </span>
  );
}

function SetupQualityDetail({ seat, testid }) {
  if (!seat) return null;
  const checks = seat.execution_checks || {};
  const entries = Object.entries(checks);
  return (
    <div
      className="border border-rd-border bg-rd-bg2 p-3 md:col-span-2"
      data-testid={testid}
    >
      <div className="flex items-center gap-2 mb-2">
        <Lightning size={11} weight="bold" className="text-rd-dim" />
        <span className="text-[10px] uppercase tracking-widest text-rd-dim">
          Setup Quality
        </span>
        <span className="text-[10px] font-mono text-rd-muted italic ml-auto">
          ADVISORY ONLY · does not gate execution
        </span>
      </div>
      {entries.length > 0 ? (
        <ul className="grid grid-cols-2 md:grid-cols-3 gap-x-3 gap-y-1">
          {entries.map(([k, v]) => (
            <li
              key={k}
              className="text-[11px] font-mono flex items-center gap-1.5"
            >
              <span
                style={{
                  color: v ? "#10B981" : "#A1A1AA",
                  fontWeight: 700,
                }}
              >
                {v ? "✓" : "✗"}
              </span>
              <span className="text-rd-text">{k.replaceAll("_", " ")}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-[11px] font-mono text-rd-dim italic">
          no checks recorded
        </div>
      )}
      {seat.lesson && (
        <div className="text-[10px] font-mono text-rd-dim italic leading-relaxed mt-2 pt-2 border-t border-rd-border">
          {seat.lesson}
        </div>
      )}
    </div>
  );
}



export default function DoctrineStrip({ packet, intentId }) {
  const [open, setOpen] = useState(false);
  if (!packet || packet.error) {
    return (
      <div
        className="px-3 py-2 bg-rd-bg border-t border-rd-border flex items-center gap-2"
        data-testid={`doctrine-strip-${intentId}-empty`}
      >
        <Warning size={11} weight="bold" className="text-rd-dim" />
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
          Doctrine
        </span>
        <span className="text-[10px] font-mono text-rd-muted">
          {packet?.error ? packet.error : "no doctrine packet attached"}
        </span>
      </div>
    );
  }

  const base = packet.base_labels || {};
  const quality = base.quality || "UNKNOWN";
  const score = base.score;
  const seats = packet.seats || {};
  const qColor = QUALITY_COLOR[quality] || "#A1A1AA";
  const qBg = QUALITY_BG[quality] || "transparent";

  return (
    <div
      className="border-t border-rd-border bg-rd-bg"
      data-testid={`doctrine-strip-${intentId}`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid={`doctrine-strip-toggle-${intentId}`}
        className="w-full px-3 py-2 flex items-center gap-3 flex-wrap text-left hover:bg-rd-bg2 transition-colors"
        title="Doctrine is read-only advisory; it does not influence execution"
      >
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-dim shrink-0">
          Doctrine
        </span>
        <span
          className="inline-flex items-center gap-1.5 px-2 py-0.5 border font-mono text-[10px] uppercase tracking-wider"
          style={{ borderColor: qColor, color: qColor, background: qBg }}
          data-testid={`doctrine-quality-${intentId}`}
        >
          {quality.replace("_", " ")}
        </span>
        <span className="font-mono text-[10px] text-rd-dim">
          score <span className="text-rd-text">{fmtNum(score, 2)}</span>
        </span>
        {packet.lane && (
          <span className="font-mono text-[10px] text-rd-dim">
            lane <span className="text-rd-text">{packet.lane}</span>
          </span>
        )}
        <div className="flex flex-wrap gap-1.5 ml-1">
          {REAL_SEATS.map((role) => (
            <SeatChip
              key={role}
              role={role}
              seat={seats[role]}
              testid={`doctrine-chip-${role}-${intentId}`}
            />
          ))}
        </div>
        {/* Setup-quality summary — ADVISORY ONLY (demoted from a peer
            seat 2026-05-31). Rendered as a small inline badge so it
            never visually implies authority. The detail card below
            still shows the per-check breakdown for transparency. */}
        {seats.execution_judge && (
          <SetupQualityBadge
            seat={seats.execution_judge}
            testid={`doctrine-setup-quality-${intentId}`}
          />
        )}
        <span className="ml-auto inline-flex items-center gap-1 text-[10px] font-mono uppercase tracking-wider text-rd-dim">
          {open ? "hide" : "details"}
          {open ? (
            <CaretUp size={11} weight="bold" />
          ) : (
            <CaretDown size={11} weight="bold" />
          )}
        </span>
      </button>

      {open && (
        <div
          className="px-3 pb-3 pt-1 grid grid-cols-1 md:grid-cols-2 gap-3"
          data-testid={`doctrine-detail-${intentId}`}
        >
          {REAL_SEATS.map((role) => (
            <SeatDetailCard key={role} role={role} seat={seats[role]} />
          ))}
          {seats.execution_judge && (
            <SetupQualityDetail
              seat={seats.execution_judge}
              testid={`doctrine-setup-quality-detail-${intentId}`}
            />
          )}
          {(base.reasons || []).length > 0 && (
            <div className="md:col-span-2 border border-rd-border bg-rd-bg2 p-3" data-testid={`doctrine-reasons-${intentId}`}>
              <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1.5">
                Base Reasons
              </div>
              <div className="flex flex-wrap gap-1.5">
                {(base.reasons || []).map((r) => (
                  <span
                    key={r}
                    className="px-1.5 py-0.5 border border-rd-border text-[10px] font-mono text-rd-muted"
                  >
                    {String(r).replaceAll("_", " ")}
                  </span>
                ))}
              </div>
            </div>
          )}
          <div className="md:col-span-2 flex items-center justify-between text-[10px] font-mono text-rd-dim">
            <span>
              doctrine_version: <span className="text-rd-text">{packet.doctrine_version || "—"}</span>
            </span>
            <span className="text-rd-muted italic">
              ADVISORY ONLY · does not influence execution
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
