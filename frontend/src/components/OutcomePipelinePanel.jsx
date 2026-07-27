import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning, ShieldCheck, ArrowsClockwise, Skull } from "@phosphor-icons/react";

function Counter({ label, value, bad, testid }) {
  return (
    <div className="border border-rd-border px-2 py-1" data-testid={testid}>
      <div className="text-[9px] uppercase tracking-widest text-rd-dim font-mono">{label}</div>
      <div className={`text-sm font-mono ${bad ? "text-red-500" : "text-rd-text"}`}>{value ?? "—"}</div>
    </div>
  );
}

function LossRow({ r }) {
  const [open, setOpen] = useState(false);
  const rMult = r.realized_r_multiple;
  return (
    <div className="border-b border-rd-border/50 py-1" data-testid={`forensic-loss-${r.plan_id}`}>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 text-[10px] font-mono text-left hover:bg-rd-border/20 px-1"
        data-testid={`forensic-loss-toggle-${r.plan_id}`}
      >
        <span className="text-red-500 w-16">{r.net_pnl != null ? `$${r.net_pnl.toFixed(2)}` : "?"}</span>
        <span className="text-rd-text w-20 truncate">{r.symbol}</span>
        <span className="text-rd-dim w-14">{r.lane}</span>
        <span className="text-rd-dim w-20 truncate">{r.brain || "unattributed"}</span>
        <span className="text-rd-dim w-14">{rMult != null ? `${rMult}R` : "no-R"}</span>
        <span className="text-rd-dim flex-1 truncate">{r.exit_reason || r.outcome}</span>
        {r.attribution_gaps?.length > 0 && (
          <span className="text-yellow-500 uppercase text-[9px]">{r.attribution_gaps.length} gaps</span>
        )}
      </button>
      {open && (
        <div className="text-[9px] font-mono text-rd-dim px-2 py-1 space-y-0.5" data-testid={`forensic-loss-detail-${r.plan_id}`}>
          <div>entry {r.entry_price ?? "?"} · stop {r.stop_price ?? "?"} · exit {r.exit_price ?? "?"} · qty {r.qty ?? "?"} · regime {r.regime || "?"}</div>
          <div>governor ×{r.governor_multiplier ?? "?"} · risk budget ${r.risk_budget ?? "?"} · projected loss ${r.projected_loss_at_stop ?? "?"} · overshoot {r.loss_overshoot_x_budget != null ? `${r.loss_overshoot_x_budget}×` : "?"}</div>
          <div>roadguard {r.roadguard ? `${r.roadguard.risk_ok ? "OK" : "BLOCKED"} (${r.roadguard.risk_reason || "-"})` : "no execution row"} · seat {r.seat_holder || "?"} · escalation {r.loss_escalation || "?"}</div>
          <div>data: {r.data_completeness ? `${r.data_completeness.enrichment_status || "?"} · bars ${r.data_completeness.bars_used ?? "?"} · missing [${(r.data_completeness.missing_required_fields || []).join(", ")}]` : "no intent row"}</div>
          {r.attribution_gaps?.length > 0 && (
            <div className="text-yellow-500">broken links: {r.attribution_gaps.join(" · ")}</div>
          )}
        </div>
      )}
    </div>
  );
}

export default function OutcomePipelinePanel() {
  const [health, setHealth] = useState(null);
  const [losses, setLosses] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [h, l] = await Promise.all([
        api.get("/admin/pipeline/outcome_health"),
        api.get("/admin/pipeline/forensics/large_losses", { params: { min_loss_usd: 20, days: 30 } }),
      ]);
      setHealth(h.data);
      setLosses(l.data);
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);

  const fail = health && !health.ok;
  return (
    <div className="border border-rd-border p-3" data-testid="outcome-pipeline-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
            Outcome Collection &amp; Attribution · 7d
          </div>
          {health && (
            <span
              data-testid="outcome-pipeline-status"
              className={`text-[10px] font-mono uppercase tracking-widest px-2 py-0.5 border ${fail ? "border-red-500 text-red-500" : "border-emerald-600 text-emerald-500"}`}
            >
              {fail ? <><Warning size={10} className="inline mr-1" />FAIL</> : <><ShieldCheck size={10} className="inline mr-1" />PASS</>}
            </span>
          )}
        </div>
        <button
          onClick={load}
          disabled={busy}
          data-testid="outcome-pipeline-refresh"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40"
        >
          <ArrowsClockwise size={10} className="inline mr-1" />refresh
        </button>
      </div>

      {err && <div className="text-[10px] font-mono text-red-500 mb-2" data-testid="outcome-pipeline-error">{err}</div>}

      {health && (
        <>
          <div className="grid grid-cols-3 sm:grid-cols-7 gap-1 mb-2">
            <Counter label="entries" value={health.entries_seen} testid="oc-entries-seen" />
            <Counter label="exits" value={health.exits_seen} testid="oc-exits-seen" />
            <Counter label="matched" value={health.matched_round_trips} testid="oc-matched" />
            <Counter label="unm. entries" value={health.unmatched_entries} bad={health.unmatched_entries > 0} testid="oc-unmatched-entries" />
            <Counter label="unm. exits" value={health.unmatched_exits} bad={health.unmatched_exits > 0} testid="oc-unmatched-exits" />
            <Counter label="resolved" value={health.resolved_outcomes} testid="oc-resolved" />
            <Counter label="no R-mult" value={health.outcomes_missing_r_multiple} bad={health.outcomes_missing_r_multiple > 0} testid="oc-missing-r" />
          </div>
          {health.alerts?.length > 0 && (
            <div className="border border-red-500/60 px-2 py-1 mb-2 space-y-0.5" data-testid="outcome-pipeline-alerts">
              {health.alerts.map((a, i) => (
                <div key={i} className="text-[10px] font-mono text-red-500">
                  <Warning size={10} className="inline mr-1" />{a}
                </div>
              ))}
            </div>
          )}
        </>
      )}

      <div className="flex items-center gap-2 mt-2 mb-1">
        <Skull size={12} className="text-rd-dim" />
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
          Loss Forensics &gt; $20 · 30d
          {losses && <span className="ml-2 text-red-500">{losses.losses} losses · ${losses.total_net_pnl?.toFixed(2)} · {losses.with_attribution_gaps} with gaps</span>}
        </div>
      </div>
      <div data-testid="forensic-losses-list">
        {losses?.reports?.length > 0
          ? losses.reports.map((r) => <LossRow key={r.plan_id || r.trade_id} r={r} />)
          : <div className="text-[10px] font-mono text-rd-dim px-1">no realized losses &gt; $20 in window</div>}
      </div>
    </div>
  );
}
