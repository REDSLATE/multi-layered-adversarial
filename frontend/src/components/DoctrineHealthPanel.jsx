import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  Shield, CheckCircle, WarningCircle, Hourglass, Eye, TrendUp, TrendDown,
  ArrowsClockwise,
} from "@phosphor-icons/react";

/**
 * DoctrineHealthPanel — live operational state of every doctrine version.
 *
 * Doctrine pin (2026-02-17, P2):
 *   This panel never describes doctrine in the abstract. It describes
 *   the doctrine's CURRENT HEALTH and the OPERATOR-VISIBLE GATE STATE.
 *   `(lane, doctrine_version)` is the scoring axis. Brain identity is
 *   not surfaced here at all — it's covered by the seat-occupancy
 *   metadata block elsewhere.
 *
 * Modes:
 *   mode="compact"  — single-row strip with metric chips + verdict.
 *   mode="full"     — card grid with ideal-snapshot + blockers + drift.
 */

const VERDICT_THEME = {
  CANDIDATE_RETIREMENT: {
    color: "#DC2626", bg: "rgba(220,38,38,0.08)",
    icon: WarningCircle, label: "Candidate · Retire",
  },
  CANDIDATE_PROMOTION: {
    color: "#10B981", bg: "rgba(16,185,129,0.08)",
    icon: CheckCircle, label: "Candidate · Promote",
  },
  WATCHING: {
    color: "#F59E0B", bg: "rgba(245,158,11,0.08)",
    icon: Eye, label: "Watching",
  },
  LEARNING: {
    color: "#3B82F6", bg: "rgba(59,130,246,0.08)",
    icon: Hourglass, label: "Learning",
  },
};

function fmtR(v, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)}R`;
}

function fmtPct(v) {
  if (v == null) return "—";
  return `${(Number(v) * 100).toFixed(1)}%`;
}

function Metric({ label, value, color, testid }) {
  return (
    <div className="flex flex-col gap-0.5" data-testid={testid}>
      <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">
        {label}
      </span>
      <span
        className="text-[12px] font-mono font-semibold"
        style={{ color: color || "var(--rd-text)" }}
      >
        {value}
      </span>
    </div>
  );
}

function ProgressBar({ value, color, testid }) {
  const pct = Math.max(0, Math.min(1, Number(value || 0))) * 100;
  return (
    <div className="w-full h-1 bg-rd-bg2 border border-rd-border overflow-hidden" data-testid={testid}>
      <div
        className="h-full transition-all"
        style={{ width: `${pct}%`, background: color || "#3B82F6" }}
      />
    </div>
  );
}

function CompactRow({ slice }) {
  const theme = VERDICT_THEME[slice.verdict] || VERDICT_THEME.LEARNING;
  const Icon = theme.icon;
  const trend = slice.expectancy_R == null
    ? null
    : (slice.expectancy_R >= 0 ? TrendUp : TrendDown);
  const TrendIcon = trend;
  return (
    <div
      className="flex items-center gap-3 px-3 py-2 border-b border-rd-border last:border-b-0 hover:bg-rd-bg2 transition-colors"
      style={{ background: theme.bg }}
      data-testid={`doctrine-health-compact-${slice.doctrine_version}`}
    >
      <Icon size={13} weight="bold" style={{ color: theme.color }} />
      <span className="font-mono text-[11px] text-rd-text min-w-[180px] truncate">
        {slice.ideal?.title || slice.doctrine_version}
      </span>
      <span
        className="font-mono text-[9px] uppercase tracking-wider px-1.5 py-0.5 border"
        style={{ borderColor: theme.color, color: theme.color }}
      >
        {theme.label}
      </span>
      <span className="font-mono text-[10px] text-rd-dim">
        {slice.samples}/100
      </span>
      <div className="w-24 hidden md:block">
        <ProgressBar value={slice.progress_to_min_samples} color={theme.color} />
      </div>
      <span className="font-mono text-[10px] text-rd-dim hidden md:inline">
        win <span className="text-rd-text">{fmtPct(slice.win_rate)}</span>
      </span>
      <span className="font-mono text-[10px] text-rd-dim flex items-center gap-1">
        exp{" "}
        {TrendIcon && (
          <TrendIcon
            size={9}
            weight="bold"
            style={{
              color: slice.expectancy_R >= 0 ? "#10B981" : "#DC2626",
            }}
          />
        )}
        <span className="text-rd-text">{fmtR(slice.expectancy_R)}</span>
      </span>
      <span className="font-mono text-[10px] text-rd-dim hidden md:inline">
        dd <span className="text-rd-text">{fmtR(slice.max_drawdown_R)}</span>
      </span>
      <span className="ml-auto text-[10px] font-mono text-rd-muted truncate max-w-[280px] hidden lg:inline">
        {(slice.blockers || []).slice(0, 1).join(" · ")}
      </span>
    </div>
  );
}

function FullCard({ slice, thresholds }) {
  const theme = VERDICT_THEME[slice.verdict] || VERDICT_THEME.LEARNING;
  const Icon = theme.icon;
  const ideal = slice.ideal || {};
  const expColor = slice.expectancy_R == null
    ? "var(--rd-text)"
    : slice.expectancy_R >= thresholds.expectancy_promotion_floor
      ? "#10B981"
      : slice.expectancy_R < thresholds.expectancy_retirement_floor
        ? "#DC2626"
        : "#F59E0B";

  return (
    <div
      className="border border-rd-border bg-rd-bg"
      data-testid={`doctrine-health-card-${slice.doctrine_version}`}
    >
      {/* Header */}
      <div
        className="px-4 py-3 border-b border-rd-border flex items-start gap-3"
        style={{ background: theme.bg }}
      >
        <Icon size={18} weight="bold" style={{ color: theme.color }} />
        <div className="flex-1 min-w-0">
          <div className="font-mono text-sm text-rd-text truncate">
            {ideal.title || slice.doctrine_version}
          </div>
          <div className="font-mono text-[10px] text-rd-dim mt-0.5">
            <span className="uppercase tracking-wider">{slice.lane}</span>
            <span className="mx-1.5">·</span>
            <span>{slice.doctrine_version}</span>
          </div>
        </div>
        <span
          className="font-mono text-[10px] uppercase tracking-wider px-2 py-0.5 border whitespace-nowrap"
          style={{ borderColor: theme.color, color: theme.color }}
          data-testid={`verdict-${slice.doctrine_version}`}
        >
          {theme.label}
        </span>
      </div>

      {/* Summary line */}
      {ideal.summary && (
        <div className="px-4 py-2 border-b border-rd-border text-[11px] font-mono text-rd-dim leading-relaxed">
          {ideal.summary}
        </div>
      )}

      {/* Metric grid */}
      <div className="px-4 py-3 grid grid-cols-2 md:grid-cols-4 gap-3 border-b border-rd-border">
        <Metric
          label="Samples"
          value={`${slice.samples} / ${thresholds.min_samples}`}
          testid={`metric-samples-${slice.doctrine_version}`}
        />
        <Metric
          label="Expectancy"
          value={fmtR(slice.expectancy_R)}
          color={expColor}
          testid={`metric-expectancy-${slice.doctrine_version}`}
        />
        <Metric
          label="Max Drawdown"
          value={fmtR(slice.max_drawdown_R)}
          color={
            slice.max_drawdown_R != null
            && slice.max_drawdown_R >= thresholds.max_drawdown_retirement_floor
              ? "#DC2626"
              : undefined
          }
          testid={`metric-drawdown-${slice.doctrine_version}`}
        />
        <Metric
          label="Win Rate"
          value={fmtPct(slice.win_rate)}
          testid={`metric-winrate-${slice.doctrine_version}`}
        />
        <Metric
          label="Consistency"
          value={
            slice.consistency == null
              ? "—"
              : fmtPct(slice.consistency)
          }
          color={
            slice.consistency != null
            && slice.consistency >= thresholds.consistency_promotion_floor
              ? "#10B981"
              : undefined
          }
          testid={`metric-consistency-${slice.doctrine_version}`}
        />
        <Metric
          label="Avg Win"
          value={
            slice.avg_win_usd != null
              ? `$${Number(slice.avg_win_usd).toFixed(2)}`
              : "—"
          }
          testid={`metric-avgwin-${slice.doctrine_version}`}
        />
        <Metric
          label="Avg Loss"
          value={
            slice.avg_loss_usd != null
              ? `$${Number(slice.avg_loss_usd).toFixed(2)}`
              : "—"
          }
          testid={`metric-avgloss-${slice.doctrine_version}`}
        />
        <div className="flex flex-col gap-1 justify-center">
          <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">
            Progress · 100 samples
          </span>
          <ProgressBar
            value={slice.progress_to_min_samples}
            color={theme.color}
            testid={`progress-${slice.doctrine_version}`}
          />
        </div>
      </div>

      {/* What it wants + blockers + common rejections */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-0 divide-y md:divide-y-0 md:divide-x divide-rd-border">
        <div className="px-4 py-3">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-center gap-1.5">
            <CheckCircle size={10} weight="bold" className="text-[#10B981]" />
            What it wants
          </div>
          <ul className="space-y-1">
            {(ideal.wants || []).map((w) => (
              <li
                key={w}
                className="text-[11px] font-mono text-rd-text flex items-start gap-1.5 leading-snug"
              >
                <span className="text-[#10B981]">✓</span>
                <span>{w}</span>
              </li>
            ))}
            {(ideal.wants || []).length === 0 && (
              <li className="text-[11px] font-mono text-rd-muted italic">
                no ideal-snapshot configured
              </li>
            )}
          </ul>
        </div>

        <div className="px-4 py-3">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-center gap-1.5">
            <Eye size={10} weight="bold" style={{ color: theme.color }} />
            Gate Blockers
          </div>
          <ul className="space-y-1">
            {(slice.blockers || []).map((b) => (
              <li
                key={b}
                className="text-[11px] font-mono text-rd-text flex items-start gap-1.5 leading-snug"
              >
                <span style={{ color: theme.color }}>›</span>
                <span>{b}</span>
              </li>
            ))}
            {(slice.blockers || []).length === 0 && (
              <li className="text-[11px] font-mono text-[#10B981] flex items-start gap-1.5 leading-snug">
                <CheckCircle size={11} weight="bold" />
                <span>all gates cleared</span>
              </li>
            )}
          </ul>
        </div>

        <div className="px-4 py-3">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-center gap-1.5">
            <WarningCircle size={10} weight="bold" className="text-[#DC2626]" />
            Common Rejections
          </div>
          <ul className="space-y-1">
            {(ideal.common_rejections || []).map((r) => (
              <li
                key={r}
                className="text-[11px] font-mono text-rd-muted flex items-start gap-1.5 leading-snug"
              >
                <span className="text-[#DC2626]">✗</span>
                <span>{r}</span>
              </li>
            ))}
            {(ideal.common_rejections || []).length === 0 && (
              <li className="text-[11px] font-mono text-rd-muted italic">
                no rejection model configured
              </li>
            )}
          </ul>
        </div>
      </div>
    </div>
  );
}

export default function DoctrineHealthPanel({ mode = "compact", lane }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = {};
      if (lane && lane !== "all") params.lane = lane;
      const res = await api.get("/admin/doctrine/promotion-status", { params });
      setData(res.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, [lane]);

  useEffect(() => { load(); }, [load]);

  const slices = useMemo(() => data?.slices || [], [data]);
  const thresholds = data?.thresholds || {
    min_samples: 100,
    expectancy_promotion_floor: 0.3,
    expectancy_retirement_floor: -0.1,
    max_drawdown_promotion_ceiling: 5.0,
    max_drawdown_retirement_floor: 8.0,
    consistency_promotion_floor: 0.55,
  };

  if (err) return null;
  if (data == null) {
    return (
      <div
        className="mb-4 px-3 py-2 border border-rd-border bg-rd-bg text-[10px] font-mono text-rd-dim"
        data-testid="doctrine-health-loading"
      >
        Loading doctrine health…
      </div>
    );
  }
  if (slices.length === 0) return null;

  if (mode === "compact") {
    return (
      <div className="mb-4" data-testid="doctrine-health-compact">
        <div className="px-3 py-2 border border-b-0 border-rd-border bg-rd-bg flex items-center gap-3">
          <Shield size={12} weight="bold" className="text-rd-dim" />
          <span className="text-[10px] uppercase tracking-widest text-rd-text font-mono">
            Doctrine Health
          </span>
          <span
            className="px-1.5 py-0.5 text-[10px] font-mono border border-rd-border text-rd-dim"
            data-testid="doctrine-health-count"
          >
            {slices.length} doctrines
          </span>
          <span className="text-[9px] font-mono italic text-rd-muted hidden md:inline">
            Expectancy-driven gate · read-only · does not influence execution
          </span>
          <button
            onClick={load}
            disabled={loading}
            data-testid="doctrine-health-reload"
            className="ml-auto p-1 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text disabled:opacity-50"
            title="Reload doctrine health"
          >
            <ArrowsClockwise size={11} weight="bold" />
          </button>
        </div>
        <div className="border border-rd-border">
          {slices.map((s) => (
            <CompactRow key={`${s.lane}/${s.doctrine_version}`} slice={s} />
          ))}
        </div>
      </div>
    );
  }

  // full mode
  return (
    <div className="space-y-4" data-testid="doctrine-health-full">
      {slices.map((s) => (
        <FullCard
          key={`${s.lane}/${s.doctrine_version}`}
          slice={s}
          thresholds={thresholds}
        />
      ))}
    </div>
  );
}
