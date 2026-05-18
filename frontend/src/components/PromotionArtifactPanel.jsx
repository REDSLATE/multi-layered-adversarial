import React, { useCallback, useEffect, useState } from "react";
import { api, RUNTIME_META } from "@/lib/api";
import { Card, Badge, LoadingRow } from "@/components/ui-bits";

/**
 * PromotionArtifactPanel — operator surface for seat-promotion evidence.
 *
 * Calls GET /api/admin/promotion-artifact?hours=24 (all-brains scan).
 * Each brain card shows:
 *   - Verdict chip (recommend_promote / keep_in_challenger / insufficient_data)
 *   - 4 metric tiles (sample size, directional agreement, MTM hit rate,
 *     simulated PnL)
 *   - One-line rationale
 *   - "Download report" button → JSON file (per-intent detail included)
 *
 * Doctrine: this panel is READ-ONLY EVIDENCE. Promotion itself still
 * flows through the Patent-J countersign at /admin/promotion/proposals.
 */

const VERDICT_META = {
  recommend_promote:   { color: "#10B981", label: "RECOMMEND PROMOTE" },
  keep_in_challenger:  { color: "#F59E0B", label: "KEEP IN CHALLENGER" },
  insufficient_data:   { color: "#A1A1AA", label: "INSUFFICIENT DATA" },
};

const HOUR_OPTIONS = [
  { value: 1,   label: "1H" },
  { value: 6,   label: "6H" },
  { value: 24,  label: "24H" },
  { value: 72,  label: "3D" },
  { value: 168, label: "7D" },
];

function pct(x) {
  if (x === null || x === undefined) return "—";
  return `${(x * 100).toFixed(1)}%`;
}

function usd(x) {
  if (x === null || x === undefined) return "—";
  const sign = x >= 0 ? "+" : "−";
  return `${sign}$${Math.abs(x).toFixed(2)}`;
}

function MetricTile({ label, value, color, testid }) {
  return (
    <div
      className="bg-rd-bg border border-rd-border px-3 py-2"
      data-testid={testid}
    >
      <div className="text-[9px] uppercase tracking-widest text-rd-dim mb-1">
        {label}
      </div>
      <div
        className="font-mono text-base font-bold"
        style={color ? { color } : undefined}
      >
        {value}
      </div>
    </div>
  );
}

function BrainReportCard({ report, onDownload }) {
  const meta = RUNTIME_META[report.brain] || { color: "#A1A1AA", label: report.brain };
  const verdict = VERDICT_META[report.verdict] || VERDICT_META.insufficient_data;
  const m = report.metrics || {};
  const pnlColor = (m.simulated_pnl_usd || 0) >= 0 ? "#10B981" : "#DC2626";
  return (
    <Card
      className="p-4"
      testid={`promo-artifact-card-${report.brain}`}
    >
      <div className="flex items-center justify-between gap-3 mb-3 pb-2 border-b border-rd-border">
        <div className="flex items-center gap-2">
          <span
            className="font-display text-lg font-black tracking-tight"
            style={{ color: meta.color }}
            data-testid={`promo-artifact-brain-${report.brain}`}
          >
            {meta.label}
          </span>
          <span className="text-[10px] font-mono uppercase text-rd-dim">
            vs {report.benchmark_brain}
          </span>
        </div>
        <Badge
          color={verdict.color}
          testid={`promo-artifact-verdict-${report.brain}`}
        >
          {verdict.label}
        </Badge>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
        <MetricTile
          label="samples"
          value={m.sample_size ?? 0}
          testid={`promo-artifact-samples-${report.brain}`}
        />
        <MetricTile
          label="agreement w/ alpha"
          value={pct(m.directional_agreement_rate)}
          testid={`promo-artifact-agreement-${report.brain}`}
        />
        <MetricTile
          label="mtm hit rate"
          value={pct(m.hit_rate_mtm)}
          testid={`promo-artifact-hitrate-${report.brain}`}
        />
        <MetricTile
          label="simulated pnl"
          value={usd(m.simulated_pnl_usd)}
          color={pnlColor}
          testid={`promo-artifact-pnl-${report.brain}`}
        />
      </div>

      <div className="text-[11px] font-mono text-rd-text leading-relaxed mb-3">
        {report.verdict_rationale}
      </div>

      <div className="flex items-center justify-between gap-2 text-[10px] font-mono text-rd-dim">
        <div>
          <span className="text-rd-text">{m.hit_rate_eligible ?? 0}</span> mtm-eligible
          {" · "}
          alpha-fill match pnl <span className="text-rd-text">{usd(m.realized_pnl_match_usd)}</span>
        </div>
        <button
          type="button"
          onClick={() => onDownload(report)}
          data-testid={`promo-artifact-download-${report.brain}`}
          className="px-2 py-1 border border-rd-border text-rd-dim hover:text-rd-text uppercase tracking-wider"
        >
          download json
        </button>
      </div>
    </Card>
  );
}

export default function PromotionArtifactPanel() {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const { data } = await api.get("/admin/promotion-artifact", { params: { hours } });
      setData(data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  }, [hours]);

  useEffect(() => { load(); }, [load]);

  const download = useCallback((report) => {
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const ts = new Date().toISOString().replace(/[:.]/g, "-");
    a.download = `promotion-artifact-${report.brain}-${ts}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, []);

  return (
    <Card className="p-0 overflow-hidden" testid="promo-artifact-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 border-b border-rd-border bg-rd-bg3">
        <div className="label-eyebrow text-rd-dim">Promotion artifact · evidence feed</div>
        <span className="text-[10px] font-mono text-rd-dim">
          shadow proposals vs alpha fills · seat-promotion verdict per brain
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {HOUR_OPTIONS.map((o) => {
            const active = o.value === hours;
            return (
              <button
                key={o.value}
                type="button"
                onClick={() => setHours(o.value)}
                data-testid={`promo-artifact-hours-${o.value}`}
                className={
                  "px-2 py-1 text-[10px] font-mono uppercase tracking-wider border " +
                  (active
                    ? "border-rd-text text-rd-text bg-rd-bg"
                    : "border-rd-border text-rd-dim hover:text-rd-text")
                }
              >
                {o.label}
              </button>
            );
          })}
          <button
            type="button"
            onClick={load}
            disabled={busy}
            data-testid="promo-artifact-reload"
            className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text disabled:opacity-40"
          >
            {busy ? "..." : "reload"}
          </button>
        </div>
      </div>

      {err && (
        <div className="px-4 py-2 text-xs font-mono text-rd-danger border-b border-rd-border">
          {err}
        </div>
      )}

      {!data && !err && <LoadingRow />}

      {data && (
        <>
          <div className="px-4 py-2 bg-rd-bg text-[10px] font-mono text-rd-dim border-b border-rd-border">
            benchmark <span className="text-rd-text">{data.benchmark_brain}</span>
            {" · window "}<span className="text-rd-text">{data.hours}h</span>
            {" · "}<span className="text-rd-text">{(data.reports || []).length}</span> brain reports
            {" · advisory only — Patent J countersign still required to promote"}
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 p-4">
            {(data.reports || []).map((r) => (
              <BrainReportCard
                key={r.brain}
                report={r}
                onDownload={download}
              />
            ))}
          </div>
        </>
      )}
    </Card>
  );
}
