import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  Warning, CaretDown, CaretUp, ArrowsClockwise, Shield, Sword, Lightning, Sparkle,
} from "@phosphor-icons/react";

/**
 * AutoRetireStrip — seat-doctrinal retirement suggestion banner.
 *
 * Doctrine pin (2026-02-17):
 *   Retirement targets (lane, seat, doctrine_version). NEVER a brain.
 *   This UI must frame every suggestion as a SEAT DOCTRINE issue —
 *   "equity/governor v1: block heuristic is underperforming". Holders
 *   are surfaced as METADATA only, in a small "while this was
 *   measured: chevelle held the seat" line. Never in the headline,
 *   never as the actor.
 */

const SEVERITY_COLOR = {
  BLAZING: "#DC2626",
  HOT: "#EF4444",
  WARM: "#F59E0B",
  FRICTION: "#FBBF24",
};

const SEVERITY_BG = {
  BLAZING: "rgba(220,38,38,0.10)",
  HOT: "rgba(239,68,68,0.10)",
  WARM: "rgba(245,158,11,0.10)",
  FRICTION: "rgba(251,191,36,0.10)",
};

const SEAT_ICON = {
  strategist: Sparkle,
  adversary: Sword,
  governor: Shield,
  execution_judge: Lightning,
};

function fmtPct(v) {
  if (v == null) return "—";
  return `${(Number(v) * 100).toFixed(1)}%`;
}

function CandidateCard({ candidate, expanded, onToggle }) {
  const SeatIcon = SEAT_ICON[candidate.seat] || Warning;
  const sevColor = SEVERITY_COLOR[candidate.severity] || "#A1A1AA";
  const sevBg = SEVERITY_BG[candidate.severity] || "transparent";
  const occ = candidate.occupancy_during_window || {};
  const occEntries = Object.entries(occ).sort((a, b) => b[1] - a[1]);

  return (
    <div
      className="border bg-rd-bg"
      style={{ borderColor: sevColor, background: sevBg }}
      data-testid={`autoretire-candidate-${candidate.lane}-${candidate.seat}-${candidate.branch}`}
    >
      <button
        type="button"
        onClick={onToggle}
        className="w-full px-3 py-2 flex items-center gap-3 text-left hover:bg-rd-bg2 transition-colors"
        data-testid={`autoretire-toggle-${candidate.lane}-${candidate.seat}-${candidate.branch}`}
      >
        <SeatIcon size={14} weight="bold" style={{ color: sevColor }} />
        <span
          className="font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 border"
          style={{ borderColor: sevColor, color: sevColor }}
        >
          {candidate.severity}
        </span>
        <span className="font-mono text-[11px] text-rd-text flex-1 truncate">
          {candidate.headline}
        </span>
        <span className="font-mono text-[10px] text-rd-dim hidden md:inline">
          n={candidate.samples}
        </span>
        <span className="font-mono text-[10px] text-rd-dim hidden md:inline">
          Δ {Number(candidate.delta).toFixed(2)}
        </span>
        {expanded ? (
          <CaretUp size={11} weight="bold" className="text-rd-dim" />
        ) : (
          <CaretDown size={11} weight="bold" className="text-rd-dim" />
        )}
      </button>
      {expanded && (
        <div
          className="px-3 pb-3 pt-1 space-y-2 border-t border-rd-border"
          data-testid={`autoretire-detail-${candidate.lane}-${candidate.seat}-${candidate.branch}`}
        >
          <div className="text-[11px] font-mono text-rd-text leading-relaxed">
            {candidate.rationale}
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[10px] font-mono">
            <div className="border border-rd-border bg-rd-bg2 px-2 py-1">
              <div className="text-rd-dim uppercase tracking-wider">lane</div>
              <div className="text-rd-text">{candidate.lane}</div>
            </div>
            <div className="border border-rd-border bg-rd-bg2 px-2 py-1">
              <div className="text-rd-dim uppercase tracking-wider">seat</div>
              <div className="text-rd-text">{candidate.seat}</div>
            </div>
            <div className="border border-rd-border bg-rd-bg2 px-2 py-1">
              <div className="text-rd-dim uppercase tracking-wider">doctrine</div>
              <div className="text-rd-text">{candidate.doctrine_version}</div>
            </div>
            <div className="border border-rd-border bg-rd-bg2 px-2 py-1">
              <div className="text-rd-dim uppercase tracking-wider">
                {candidate.branch} vs {candidate.comparator}
              </div>
              <div className="text-rd-text">
                {fmtPct(candidate.branch_loss_rate)} → {fmtPct(candidate.comparator_loss_rate)}
              </div>
            </div>
          </div>
          <div className="border border-rd-border bg-rd-bg2 px-3 py-2">
            <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1">
              Suggested Action
            </div>
            <div className="text-[11px] font-mono text-rd-text">
              {candidate.suggested_action}
            </div>
          </div>
          <div className="border border-rd-border bg-rd-bg2 px-3 py-2">
            <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1.5 flex items-baseline gap-2">
              <span>Holder Occupancy</span>
              <span className="text-[9px] italic text-rd-muted normal-case tracking-normal">
                metadata only · NOT a scoring axis
              </span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {occEntries.length === 0 ? (
                <span className="text-[10px] text-rd-muted">no holders recorded</span>
              ) : (
                occEntries.map(([brain, n]) => (
                  <span
                    key={brain}
                    className="px-1.5 py-0.5 border border-rd-border text-[10px] font-mono text-rd-text"
                  >
                    {brain} <span className="text-rd-dim">×{n}</span>
                  </span>
                ))
              )}
            </div>
            <div className="text-[9px] italic text-rd-muted mt-1.5">
              Performance belongs to the seat doctrine, not to whoever held the seat.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function AutoRetireStrip({ lane }) {
  const [candidates, setCandidates] = useState(null);
  const [err, setErr] = useState("");
  const [expandedKey, setExpandedKey] = useState(null);
  const [collapsedAll, setCollapsedAll] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = { min_samples: 50 };
      if (lane && lane !== "all") params.lane = lane;
      const res = await api.get("/admin/doctrine/retirement-candidates", { params });
      setCandidates(res.data?.candidates || []);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, [lane]);

  useEffect(() => { load(); }, [load]);

  const visible = useMemo(() => candidates || [], [candidates]);

  if (err) {
    return null; // silent — operator can still see the doctrine in scorecards
  }
  if (candidates === null) {
    return (
      <div
        className="mb-4 px-3 py-2 border border-rd-border bg-rd-bg text-[10px] font-mono text-rd-dim"
        data-testid="autoretire-loading"
      >
        Loading retirement suggestions…
      </div>
    );
  }
  if (visible.length === 0) {
    return null; // no underperformers — nothing to suggest
  }

  return (
    <div className="mb-4" data-testid="autoretire-strip">
      <div
        className="px-3 py-2 border-b-0 border border-rd-border bg-rd-bg flex items-center gap-3"
      >
        <Warning
          size={12}
          weight="bold"
          style={{ color: SEVERITY_COLOR.BLAZING }}
        />
        <span className="text-[10px] uppercase tracking-widest text-rd-text font-mono">
          Seat-Doctrine Auto-Retire Suggestions
        </span>
        <span
          className="px-1.5 py-0.5 text-[10px] font-mono border border-rd-border text-rd-dim"
          data-testid="autoretire-count"
        >
          {visible.length} flagged
        </span>
        <span className="text-[9px] font-mono italic text-rd-muted hidden md:inline">
          Targets (lane, seat, doctrine_version) — never brain identity.
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={() => setCollapsedAll((v) => !v)}
            data-testid="autoretire-collapse"
            className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
          >
            {collapsedAll ? "expand all" : "collapse"}
          </button>
          <button
            onClick={load}
            disabled={loading}
            data-testid="autoretire-reload"
            className="p-1 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text disabled:opacity-50"
            title="Reload retirement suggestions"
          >
            <ArrowsClockwise size={11} weight="bold" />
          </button>
        </div>
      </div>
      {!collapsedAll && (
        <div className="border border-rd-border border-t-0 divide-y divide-rd-border">
          {visible.map((c) => {
            const key = `${c.lane}/${c.seat}/${c.doctrine_version}/${c.branch}`;
            return (
              <CandidateCard
                key={key}
                candidate={c}
                expanded={expandedKey === key}
                onToggle={() => setExpandedKey((k) => (k === key ? null : key))}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
