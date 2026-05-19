import React, { useState } from "react";
import {
  CaretDown, CaretUp, Sparkle, Sword, Shield, Lightning, Warning,
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
  adversary: Sword,
  governor: Shield,
  execution_judge: Lightning,
};

const SEAT_LABEL = {
  strategist: "Strategist",
  adversary: "Adversary",
  governor: "Governor",
  execution_judge: "Execution Judge",
};

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
    const ready = !!seat.execution_ready;
    return {
      text: ready ? "READY" : "not ready",
      color: ready ? "#10B981" : "#F59E0B",
      flagged: !ready,
    };
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
          {["strategist", "adversary", "governor", "execution_judge"].map((role) => (
            <SeatChip
              key={role}
              role={role}
              seat={seats[role]}
              testid={`doctrine-chip-${role}-${intentId}`}
            />
          ))}
        </div>
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
          {["strategist", "adversary", "governor", "execution_judge"].map((role) => (
            <SeatDetailCard key={role} role={role} seat={seats[role]} />
          ))}
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
