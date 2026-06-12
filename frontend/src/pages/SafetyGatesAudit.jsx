import React, { useEffect, useState } from "react";
import { PageHeader } from "@/components/ui-bits";
import { ShieldChevron, ArrowsClockwise } from "@phosphor-icons/react";
import { useAuth } from "@/context/AuthContext";

const WINDOWS = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
  { label: "30d", hours: 720 },
  { label: "ALL", hours: 0 },
];

function rateColor(rate) {
  if (rate === null || rate === undefined) return "var(--rd-dim)";
  if (rate >= 0.5) return "#ef4444";
  if (rate >= 0.25) return "#f59e0b";
  return "#22c55e";
}

function GateRow({ gate }) {
  const total = gate.total_checks || 0;
  const blockRate = gate.block_rate;
  const ratePct = blockRate === null ? "—" : `${(blockRate * 100).toFixed(1)}%`;
  return (
    <div
      data-testid={`gate-row-${gate.gate}`}
      className="border border-rd-border bg-rd-bg2 p-4 mb-3"
      style={{ borderLeft: `3px solid ${rateColor(blockRate)}` }}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-3 mb-2">
        <div className="text-[14px] font-mono text-rd-text">{gate.gate}</div>
        <div className="flex items-center gap-4 text-[11px] font-mono">
          <span className="text-rd-dim">
            checks: <span className="text-rd-text">{total}</span>
          </span>
          <span className="text-rd-dim">
            pass:{" "}
            <span style={{ color: "#22c55e" }}>{gate.pass_count}</span>
          </span>
          <span className="text-rd-dim">
            block:{" "}
            <span style={{ color: "#ef4444" }}>{gate.block_count}</span>
          </span>
          <span className="text-rd-dim">
            block_rate:{" "}
            <span style={{ color: rateColor(blockRate), fontWeight: 600 }}>
              {ratePct}
            </span>
          </span>
        </div>
      </div>

      {gate.top_block_reasons && gate.top_block_reasons.length > 0 && (
        <div className="mt-2">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-1">
            Top Block Reasons
          </div>
          {gate.top_block_reasons.map((b) => (
            <div
              key={b.reason_prefix}
              className="text-[11px] font-mono text-rd-text pl-3 relative before:content-['—'] before:absolute before:left-0 before:text-rd-dim"
            >
              <span className="text-rd-dim">[{b.count}]</span> {b.reason_prefix}
            </div>
          ))}
        </div>
      )}

      {gate.block_reason_samples && gate.block_reason_samples.length > 0 && (
        <details className="mt-2">
          <summary className="text-[9px] font-mono uppercase tracking-widest text-rd-dim cursor-pointer">
            Sample Block Messages ({gate.block_reason_samples.length})
          </summary>
          <div className="mt-1 pl-2 border-l border-rd-border">
            {gate.block_reason_samples.map((s) => (
              <div
                key={`${s.ts || ""}-${s.reason || ""}`}
                className="text-[10px] font-mono text-rd-text py-1 leading-relaxed"
              >
                <span className="text-rd-dim">{s.ts?.slice(11, 19)}</span>{" "}
                {s.reason}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function WindowPill({ label, hours, current, onClick }) {
  const active = hours === current;
  return (
    <button
      data-testid={`safety-window-${label}`}
      onClick={() => onClick(hours)}
      className="px-3 py-1 text-[11px] font-mono uppercase tracking-wider border transition-colors"
      style={{
        borderColor: active ? "var(--rd-text)" : "var(--rd-border)",
        color: active ? "var(--rd-text)" : "var(--rd-dim)",
        background: active ? "var(--rd-bg2)" : "transparent",
      }}
    >
      {label}
    </button>
  );
}

export default function SafetyGatesAudit() {
  const { token } = useAuth();
  const [hours, setHours] = useState(24);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = async (h) => {
    setLoading(true);
    setError(null);
    try {
      const url = `${process.env.REACT_APP_BACKEND_URL}/api/admin/safety-gates/audit?hours=${h}&sample_size=5`;
      const r = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(hours);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, hours]);

  return (
    <div data-testid="safety-gates-audit-page">
      <PageHeader
        icon={ShieldChevron}
        title="Safety Gates Audit"
        subtitle={
          "Per-gate pass / block stats from `shared_gate_results`. Use the "
          + "block-rate column + top-block-reasons to decide which of the four "
          + "operator gates to relax, keep, or tighten before market open."
        }
        testid="safety-gates-audit-header"
      />

      <div className="mb-4 flex items-center gap-2 flex-wrap">
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
          Window
        </span>
        {WINDOWS.map((w) => (
          <WindowPill
            key={w.label}
            label={w.label}
            hours={w.hours}
            current={hours}
            onClick={setHours}
          />
        ))}
        <button
          onClick={() => load(hours)}
          disabled={loading}
          data-testid="safety-refresh-btn"
          className="ml-auto flex items-center gap-2 px-3 py-1 text-[11px] font-mono uppercase tracking-wider border border-rd-border text-rd-text hover:bg-rd-bg2 transition-colors disabled:opacity-50"
        >
          <ArrowsClockwise size={14} />
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {data && (
        <div
          className="mb-4 p-3 border border-rd-border bg-rd-bg2 grid grid-cols-2 md:grid-cols-4 gap-4"
          data-testid="safety-audit-summary"
        >
          <div>
            <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">
              Rows Scanned
            </div>
            <div className="text-[16px] font-mono text-rd-text">
              {data.rows_scanned?.toLocaleString()}
            </div>
          </div>
          <div>
            <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">
              Target-Gate Decisions
            </div>
            <div className="text-[16px] font-mono text-rd-text">
              {data.decisions_against_target_gates?.toLocaleString()}
            </div>
          </div>
          <div>
            <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">
              Verdict Mix
            </div>
            <div className="text-[11px] font-mono text-rd-text leading-tight">
              {Object.entries(data.verdict_counts || {}).map(([k, v]) => (
                <div key={k}>
                  {k}: <span className="text-rd-dim">{v}</span>
                </div>
              ))}
            </div>
          </div>
          <div>
            <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">
              Generated
            </div>
            <div className="text-[11px] font-mono text-rd-text">
              {data.generated_at?.slice(11, 19)}Z
            </div>
          </div>
        </div>
      )}

      {error && (
        <div
          className="text-[11px] font-mono p-3 border border-red-700 bg-red-950/40 text-red-300"
          data-testid="safety-error"
        >
          {error}
        </div>
      )}

      {data?.gates?.length > 0 && (
        <div data-testid="safety-gates-list">
          {data.gates.map((g) => (
            <GateRow key={g.gate} gate={g} />
          ))}
        </div>
      )}

      {data && data.gates?.every((g) => g.total_checks === 0) && (
        <div
          className="text-[11px] font-mono text-rd-dim p-4 border border-rd-border bg-rd-bg2"
          data-testid="safety-no-data"
        >
          No gate decisions recorded in this window.
        </div>
      )}
    </div>
  );
}
