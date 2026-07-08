/**
 * FingerprintDiffPanel — before/after doctrine-change validation.
 *
 * Loads two ranges of session fingerprints, aggregates each side into
 * a composite, and renders the deltas. Meant for the operator question:
 * "I changed threshold X at time T. Did the funnel shift as expected,
 * or did I accidentally starve a lane?"
 *
 * Reads from GET /api/admin/fingerprints/diff — no writes.
 */
import React, { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui-bits";

const BRAINS = ["camino", "barracuda", "hellcat", "gto"];
const LANES = ["equity", "crypto"];

/**
 * Default: BEFORE = [pivot - windowH, pivot], AFTER = [pivot, pivot + windowH].
 * `pivot` defaults to now. `windowH` defaults to 2h.
 */
function defaultRanges(pivotIso, windowH) {
  const pivot = pivotIso ? new Date(pivotIso) : new Date();
  const beforeStart = new Date(pivot.getTime() - windowH * 3600 * 1000);
  const afterEnd = new Date(pivot.getTime() + windowH * 3600 * 1000);
  return {
    before_start_ts: beforeStart.toISOString(),
    before_end_ts: pivot.toISOString(),
    after_start_ts: pivot.toISOString(),
    after_end_ts: afterEnd.toISOString(),
  };
}

function fmtDelta(v, digits = 3) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)}`;
}

function deltaColor(v) {
  if (v === null || v === undefined) return "text-rd-dim";
  const n = Number(v);
  if (n > 0.0001) return "text-rd-good";
  if (n < -0.0001) return "text-rd-danger";
  return "text-rd-dim";
}

function DeltaRow({ label, value, digits = 3, testid }) {
  return (
    <div className="flex items-center justify-between px-3 py-1 border-b border-rd-border">
      <span className="text-[11px] font-mono text-rd-dim">{label}</span>
      <span
        className={`text-[11px] font-mono ${deltaColor(value)}`}
        data-testid={testid}
      >
        {fmtDelta(value, digits)}
      </span>
    </div>
  );
}

function DistBlock({ title, dist, testid }) {
  const entries = Object.entries(dist || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  return (
    <div>
      <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim px-3 pt-2 pb-1">
        {title}
      </div>
      {entries.length === 0 ? (
        <div className="text-[11px] font-mono text-rd-dim px-3 pb-2">—</div>
      ) : (
        entries.map(([k, v]) => (
          <DeltaRow
            key={k}
            label={k}
            value={v}
            testid={`${testid}-${k}`}
          />
        ))
      )}
    </div>
  );
}

function TopReasonsBlock({ title, block, testid }) {
  if (!block) return null;
  const news = block.new_in_after || [];
  const drops = block.dropped_from_before || [];
  const deltas = Object.entries(block.count_deltas || {})
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
    .slice(0, 10);
  return (
    <div>
      <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim px-3 pt-2 pb-1">
        {title}
      </div>
      {news.length > 0 && (
        <div className="text-[11px] font-mono text-rd-good px-3 py-1" data-testid={`${testid}-new`}>
          NEW: {news.join(", ")}
        </div>
      )}
      {drops.length > 0 && (
        <div className="text-[11px] font-mono text-rd-danger px-3 py-1" data-testid={`${testid}-dropped`}>
          DROPPED: {drops.join(", ")}
        </div>
      )}
      {deltas.length === 0 && news.length === 0 && drops.length === 0 && (
        <div className="text-[11px] font-mono text-rd-dim px-3 py-1">—</div>
      )}
      {deltas.map(([k, v]) => (
        <div
          key={k}
          className="flex items-center justify-between px-3 py-0.5 border-b border-rd-border/40"
        >
          <span className="text-[11px] font-mono text-rd-dim truncate max-w-[70%]">{k}</span>
          <span
            className={`text-[11px] font-mono ${deltaColor(v)}`}
            data-testid={`${testid}-count-${k}`}
          >
            {v > 0 ? `+${v}` : v}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function FingerprintDiffPanel() {
  const [brain, setBrain] = useState("camino");
  const [lane, setLane] = useState("equity");
  const [pivot, setPivot] = useState("");
  const [windowH, setWindowH] = useState(2);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState(null);

  const run = useCallback(async () => {
    setLoading(true);
    setErr("");
    setResult(null);
    try {
      const ranges = defaultRanges(pivot || null, windowH);
      const { data } = await api.get("/admin/fingerprints/diff", {
        params: { brain, lane, ...ranges, top_k: 8 },
      });
      setResult(data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, [brain, lane, pivot, windowH]);

  const before = result?.before || null;
  const after = result?.after || null;
  const deltas = result?.deltas || null;

  return (
    <Card className="p-0 overflow-hidden" testid="fingerprint-diff-panel">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3 border-b border-rd-border bg-rd-bg3">
        <div className="label-eyebrow text-rd-dim">
          Fingerprint diff · before / after doctrine change
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2 text-[10px] font-mono">
          <label className="text-rd-dim">brain</label>
          <select
            value={brain}
            onChange={(e) => setBrain(e.target.value)}
            className="bg-rd-bg border border-rd-border px-2 py-1 text-rd-text"
            data-testid="fp-diff-brain-select"
          >
            {BRAINS.map((b) => <option key={b} value={b}>{b}</option>)}
          </select>
          <label className="text-rd-dim">lane</label>
          <select
            value={lane}
            onChange={(e) => setLane(e.target.value)}
            className="bg-rd-bg border border-rd-border px-2 py-1 text-rd-text"
            data-testid="fp-diff-lane-select"
          >
            {LANES.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
          <label className="text-rd-dim">pivot (UTC ISO, blank=now)</label>
          <input
            type="text"
            value={pivot}
            placeholder="2026-07-08T13:00:00Z"
            onChange={(e) => setPivot(e.target.value)}
            className="bg-rd-bg border border-rd-border px-2 py-1 text-rd-text w-56"
            data-testid="fp-diff-pivot-input"
          />
          <label className="text-rd-dim">± hours</label>
          <input
            type="number"
            min="0.25"
            step="0.25"
            value={windowH}
            onChange={(e) => setWindowH(Number(e.target.value) || 2)}
            className="bg-rd-bg border border-rd-border px-2 py-1 text-rd-text w-16"
            data-testid="fp-diff-window-input"
          />
          <button
            onClick={run}
            disabled={loading}
            className="border border-rd-border px-3 py-1 hover:bg-rd-bg text-rd-text"
            data-testid="fp-diff-run-btn"
          >
            {loading ? "..." : "compute diff"}
          </button>
        </div>
      </div>

      {err && (
        <div
          className="px-4 py-2 text-xs font-mono text-rd-danger border-b border-rd-border"
          data-testid="fp-diff-error"
        >
          {err}
        </div>
      )}

      {!result && !err && !loading && (
        <div className="px-4 py-6 text-center text-rd-dim font-mono text-xs">
          pick brain / lane / pivot, then compute — BEFORE = [pivot−Nh, pivot], AFTER = [pivot, pivot+Nh]
        </div>
      )}

      {result && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-0 border-b border-rd-border">
          <div className="p-3 border-r border-rd-border" data-testid="fp-diff-before">
            <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
              BEFORE · {before?.start_ts?.slice(11, 16)}–{before?.end_ts?.slice(11, 16)}
            </div>
            <div className="text-lg font-mono text-rd-text mt-1">
              n={before?.intent_count} · windows={before?.windows_used}
            </div>
            <div className="text-[11px] font-mono text-rd-dim mt-0.5">
              exec_ready={before?.execution_ready_rate?.toFixed(3)} · risk_p50={before?.risk_multiplier_p50 ?? "—"}
            </div>
          </div>
          <div className="p-3 border-r border-rd-border" data-testid="fp-diff-after">
            <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
              AFTER · {after?.start_ts?.slice(11, 16)}–{after?.end_ts?.slice(11, 16)}
            </div>
            <div className="text-lg font-mono text-rd-text mt-1">
              n={after?.intent_count} · windows={after?.windows_used}
            </div>
            <div className="text-[11px] font-mono text-rd-dim mt-0.5">
              exec_ready={after?.execution_ready_rate?.toFixed(3)} · risk_p50={after?.risk_multiplier_p50 ?? "—"}
            </div>
          </div>
          <div className="p-3" data-testid="fp-diff-headline">
            <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
              HEADLINE Δ
            </div>
            <div
              className={`text-lg font-mono mt-1 ${deltaColor(deltas?.execution_ready_rate)}`}
              data-testid="fp-diff-headline-exec-ready"
            >
              exec_ready {fmtDelta(deltas?.execution_ready_rate, 3)}
            </div>
            <div className={`text-[11px] font-mono mt-0.5 ${deltaColor(deltas?.intent_count)}`}>
              intent_count {fmtDelta(deltas?.intent_count, 0)} · risk_p50 {fmtDelta(deltas?.risk_multiplier_p50, 3)}
            </div>
          </div>
        </div>
      )}

      {result && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-0">
          <div className="border-r border-rd-border" data-testid="fp-diff-gate-pass-rates">
            <DistBlock
              title="Δ gate_pass_rates"
              dist={deltas?.gate_pass_rates}
              testid="fp-diff-gate-pass"
            />
            <DistBlock
              title="Δ quality_dist (pct)"
              dist={deltas?.quality_dist_pct}
              testid="fp-diff-quality"
            />
            <DistBlock
              title="Δ gate_state_dist (pct)"
              dist={deltas?.gate_state_dist_pct}
              testid="fp-diff-gate-state"
            />
          </div>
          <div className="border-r border-rd-border" data-testid="fp-diff-percentiles">
            <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim px-3 pt-2 pb-1">
              Δ confidence percentiles
            </div>
            <DeltaRow label="p10" value={deltas?.confidence_percentiles?.p10} testid="fp-diff-conf-p10" />
            <DeltaRow label="p50" value={deltas?.confidence_percentiles?.p50} testid="fp-diff-conf-p50" />
            <DeltaRow label="p90" value={deltas?.confidence_percentiles?.p90} testid="fp-diff-conf-p90" />
            <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim px-3 pt-3 pb-1">
              Δ rvol percentiles
            </div>
            <DeltaRow label="p10" value={deltas?.rvol_percentiles?.p10} testid="fp-diff-rvol-p10" />
            <DeltaRow label="p50" value={deltas?.rvol_percentiles?.p50} testid="fp-diff-rvol-p50" />
            <DeltaRow label="p90" value={deltas?.rvol_percentiles?.p90} testid="fp-diff-rvol-p90" />
            <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim px-3 pt-3 pb-1">
              Δ gap_pct percentiles
            </div>
            <DeltaRow label="p10" value={deltas?.gap_pct_percentiles?.p10} testid="fp-diff-gap-p10" />
            <DeltaRow label="p50" value={deltas?.gap_pct_percentiles?.p50} testid="fp-diff-gap-p50" />
            <DeltaRow label="p90" value={deltas?.gap_pct_percentiles?.p90} testid="fp-diff-gap-p90" />
          </div>
          <div data-testid="fp-diff-top-reasons">
            <TopReasonsBlock
              title="Δ top_fail_reasons"
              block={deltas?.top_fail_reasons}
              testid="fp-diff-fail"
            />
            <TopReasonsBlock
              title="Δ top_objections"
              block={deltas?.top_objections}
              testid="fp-diff-obj"
            />
            <TopReasonsBlock
              title="Δ top_labels"
              block={deltas?.top_labels}
              testid="fp-diff-labels"
            />
          </div>
        </div>
      )}

      {result?.note && (
        <div className="px-3 py-2 border-t border-rd-border text-[10px] font-mono text-rd-dim">
          {result.note}
        </div>
      )}
    </Card>
  );
}
