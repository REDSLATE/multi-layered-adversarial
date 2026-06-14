import React, { useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import {
  Eye, Shield, ShieldCheck, Lightning, TestTube,
  ArrowsClockwise, CheckCircle, XCircle, Warning, Stop, Play,
} from "@phosphor-icons/react";
import { useParadoxV2State } from "./useParadoxV2State";

/**
 * ParadoxV2DashboardPanel — operator-facing dashboard for the new
 * seat-owned execution doctrine (2026-02-19).
 *
 * Surfaces:
 *   - The five-layer doctrine banner (Brain / Seat / Governor /
 *     RoadGuard / Verifier — who owns what).
 *   - Per-seat policy table (autonomy mode, capital, gates) with a
 *     single-action "promote/demote autonomy" button.
 *   - Trust list — which brains each seat trusts.
 *   - Active RoadGuard stops (raise / clear).
 *   - Recent /api/v2/evaluate receipts feed so the operator can watch
 *     the pipeline make decisions in real time.
 *   - Inline test-fire form — runs a synthetic opinion through
 *     /api/v2/evaluate without touching the live intent pipeline.
 *
 * The whole panel is read-mostly; the few mutate actions
 * (autonomy promote, roadguard raise/clear, test-fire) require a
 * typed audit reason (≥4 chars) where the backend demands it.
 *
 * NOT wired into the live trading flow yet — this is the validation
 * surface for the next 50 manual evaluations before we flip the wire.
 */
export default function ParadoxV2DashboardPanel() {
  const { data, err, loading, load } = useParadoxV2State();
  const [busy, setBusy] = useState(false);

  // Test-fire form state
  const [tfSeat, setTfSeat] = useState("equity_executor");
  const [tfBrain, setTfBrain] = useState("alpha");
  const [tfSymbol, setTfSymbol] = useState("AAPL");
  const [tfLane, setTfLane] = useState("equity");
  const [tfConf, setTfConf] = useState("0.90");
  const [tfNotional, setTfNotional] = useState("2000");
  const [tfSpread, setTfSpread] = useState("");
  const [tfRvol, setTfRvol] = useState("");
  const [tfEarnings, setTfEarnings] = useState(false);
  const [tfResult, setTfResult] = useState(null);

  const fireTest = async () => {
    const evidence = {};
    if (tfSpread) evidence.spread_bps = parseFloat(tfSpread);
    if (tfRvol) evidence.rvol = parseFloat(tfRvol);
    if (tfEarnings) evidence.earnings_within_days = 3;
    setBusy(true);
    try {
      const res = await api.post("/v2/evaluate", {
        seat_id: tfSeat,
        brain_id: tfBrain,
        symbol: tfSymbol.toUpperCase(),
        lane: tfLane,
        action: "BUY",
        confidence: parseFloat(tfConf),
        suggested_notional_usd: parseFloat(tfNotional),
        evidence,
      });
      setTfResult(res.data);
      toast.success(`v2/evaluate → ${res.data.decision}`);
      load();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      toast.error(typeof d === "string" ? d : "evaluate failed");
    } finally {
      setBusy(false);
    }
  };

  const patchAutonomy = async (seatId, next) => {
    const reason = window.prompt(
      `Autonomy change for ${seatId}: → ${next}\nType audit reason (≥4 chars):`,
      "",
    );
    if (!reason || reason.trim().length < 4) return;
    setBusy(true);
    try {
      await api.patch(`/v2/seat-policy/${seatId}`, {
        autonomy_mode: next,
        reason: reason.trim(),
      });
      toast.success(`${seatId} → ${next}`);
      load();
    } catch (e) {
      toast.error("autonomy patch failed");
    } finally {
      setBusy(false);
    }
  };

  const raiseStop = async (seatId) => {
    const reason = window.prompt(`Raise RoadGuard STOP on ${seatId}:`, "");
    if (!reason || reason.trim().length < 4) return;
    setBusy(true);
    try {
      await api.post("/v2/roadguard/raise", { seat_id: seatId, reason: reason.trim() });
      toast.success(`STOP raised on ${seatId}`);
      load();
    } finally {
      setBusy(false);
    }
  };

  const clearStop = async (seatId) => {
    const reason = window.prompt(`Clear all RoadGuard stops on ${seatId}:`, "");
    if (!reason || reason.trim().length < 4) return;
    setBusy(true);
    try {
      const r = await api.post("/v2/roadguard/clear", { seat_id: seatId, reason: reason.trim() });
      toast.success(`Cleared ${r.data.cleared} stop(s) on ${seatId}`);
      load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="border-2 border-rd-accent bg-rd-bg2 p-3 space-y-3"
      data-testid="paradox-v2-dashboard"
    >
      <div className="flex items-center gap-2">
        <ShieldCheck size={14} weight="bold" className="text-rd-accent" />
        <span className="text-[11px] font-mono uppercase tracking-widest text-rd-text font-bold">
          Paradox v2 · Seat-Owned Execution
        </span>
        <span className="ml-2 text-[10px] font-mono text-rd-dim italic">
          stand-alone — not wired into live intents yet
        </span>
        <button
          onClick={load}
          disabled={loading}
          className="ml-auto p-1 border border-rd-border text-rd-dim hover:text-rd-text"
          data-testid="paradox-v2-reload"
        >
          <ArrowsClockwise size={11} weight="bold" className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {err && (
        <div className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5">
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          {err}
        </div>
      )}

      <div className="font-mono text-[10px] text-rd-dim italic border-l-2 border-rd-accent pl-2">
        {data.doctrine}
      </div>

      {/* Seat policies */}
      <div className="space-y-1" data-testid="paradox-v2-seats">
        <div className="font-mono text-[9px] uppercase text-rd-dim">Seats</div>
        <div className="border border-rd-border bg-rd-bg overflow-hidden">
          <table className="w-full font-mono text-[10px]">
            <thead className="bg-rd-bg2 text-rd-dim uppercase text-[9px]">
              <tr>
                <th className="px-2 py-1 text-left">Seat</th>
                <th className="px-2 py-1 text-left">Instrument</th>
                <th className="px-2 py-1 text-left">Autonomy</th>
                <th className="px-2 py-1 text-right">Max $</th>
                <th className="px-2 py-1 text-right">Size×</th>
                <th className="px-2 py-1 text-right">Conf≥</th>
                <th className="px-2 py-1 text-left">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.seat_policies.map((p) => (
                <tr key={p.seat_id} className="border-t border-rd-border" data-testid={`seat-row-${p.seat_id}`}>
                  <td className="px-2 py-1 text-rd-text font-bold">{p.seat_id}</td>
                  <td className="px-2 py-1">
                    <span
                      className="text-[9px] uppercase tracking-wider text-rd-dim"
                      data-testid={`instrument-${p.seat_id}`}
                    >
                      {p.instrument_type || "—"}
                    </span>
                  </td>
                  <td className="px-2 py-1">
                    <span className={
                      p.autonomy_mode === "auto_execute" ? "text-rd-success" :
                      p.autonomy_mode === "toehold" ? "text-rd-accent" :
                      p.autonomy_mode === "shadow" ? "text-rd-warn" :
                      "text-rd-dim"
                    }>
                      {p.autonomy_mode}
                    </span>
                  </td>
                  <td className="px-2 py-1 text-right text-rd-text">${p.max_notional_usd}</td>
                  <td className="px-2 py-1 text-right text-rd-text">{p.size_multiplier}</td>
                  <td className="px-2 py-1 text-right text-rd-text">{p.confidence_min}</td>
                  <td className="px-2 py-1 space-x-1">
                    {["observe", "shadow", "toehold", "auto_execute"].map((m) => (
                      m !== p.autonomy_mode && (
                        <button
                          key={m}
                          disabled={busy}
                          onClick={() => patchAutonomy(p.seat_id, m)}
                          className="text-[9px] underline-offset-2 hover:underline text-rd-dim hover:text-rd-text"
                          data-testid={`promote-${p.seat_id}-${m}`}
                        >
                          → {m}
                        </button>
                      )
                    ))}
                    <button
                      disabled={busy}
                      onClick={() => raiseStop(p.seat_id)}
                      className="ml-1 text-[9px] text-rd-danger hover:text-rd-danger/80"
                      data-testid={`raise-stop-${p.seat_id}`}
                      title="Raise RoadGuard STOP"
                    >
                      <Stop size={9} weight="bold" className="inline" /> STOP
                    </button>
                    <button
                      disabled={busy}
                      onClick={() => clearStop(p.seat_id)}
                      className="text-[9px] text-rd-success hover:text-rd-success/80"
                      data-testid={`clear-stop-${p.seat_id}`}
                      title="Clear all STOPs"
                    >
                      <Play size={9} weight="bold" className="inline" /> CLEAR
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Promotion Readiness — operator-driven promotion gate (25+ evals) */}
      <PromotionReadinessStrip onPromote={patchAutonomy} busy={busy} />

      {/* Trust + active stops + governor rules: 3 columns */}
      <div className="grid grid-cols-3 gap-2 font-mono text-[10px]">
        <div className="border border-rd-border bg-rd-bg p-2" data-testid="paradox-v2-trust">
          <div className="text-[9px] uppercase text-rd-dim mb-1">Trust list</div>
          {data.trust.length === 0 && <div className="text-rd-dim italic">none</div>}
          {data.trust.map((t) => (
            <div key={`${t.seat_id}:${t.brain_id}`} className="flex justify-between">
              <span className="text-rd-text">{t.seat_id}</span>
              <span className="text-rd-dim">←</span>
              <span className="text-rd-text">{t.brain_id}</span>
              <span className="text-rd-dim ml-1">×{t.trust_level}</span>
            </div>
          ))}
        </div>
        <div className="border border-rd-border bg-rd-bg p-2" data-testid="paradox-v2-stops">
          <div className="text-[9px] uppercase text-rd-dim mb-1">
            <Stop size={9} weight="bold" className="inline mr-1" />
            Active RoadGuard stops
          </div>
          {data.active_stops.length === 0 && <div className="text-rd-success italic">none — all clear</div>}
          {data.active_stops.map((s) => (
            <div key={s.stop_id} className="text-rd-danger">
              {s.seat_id}: {s.reason}
            </div>
          ))}
        </div>
        <div className="border border-rd-border bg-rd-bg p-2" data-testid="paradox-v2-governor">
          <div className="text-[9px] uppercase text-rd-dim mb-1">Governor rules</div>
          {data.governor_rules.map((g) => (
            <div key={g.rule_id} className="flex justify-between">
              <span className="text-rd-text">{g.trigger_type}</span>
              <span className="text-rd-dim">×{g.size_multiplier}</span>
              {g.vote_required && <span className="text-rd-warn ml-1">vote</span>}
            </div>
          ))}
        </div>
      </div>

      {/* Test-fire ── inline /v2/evaluate runner */}
      <details className="border border-rd-border bg-rd-bg p-2" data-testid="paradox-v2-test-fire">
        <summary className="cursor-pointer font-mono text-[10px] text-rd-dim hover:text-rd-text">
          <TestTube size={10} weight="bold" className="inline mr-1" />
          Test-fire /v2/evaluate (synthetic opinion, never hits broker) ▾
        </summary>
        <div className="grid grid-cols-4 gap-2 mt-2 font-mono text-[10px]">
          <label className="text-rd-dim">
            seat
            <select
              value={tfSeat}
              onChange={(e) => setTfSeat(e.target.value)}
              className="w-full bg-rd-bg2 border border-rd-border px-1 py-0.5 text-rd-text"
              data-testid="tf-seat"
            >
              {data.seat_policies.map((p) => (
                <option key={p.seat_id} value={p.seat_id}>{p.seat_id}</option>
              ))}
            </select>
          </label>
          <label className="text-rd-dim">
            brain
            <select
              value={tfBrain}
              onChange={(e) => setTfBrain(e.target.value)}
              className="w-full bg-rd-bg2 border border-rd-border px-1 py-0.5 text-rd-text"
              data-testid="tf-brain"
            >
              {data.brains.map((b) => (
                <option key={b.brain_id} value={b.brain_id}>{b.brain_id} ({b.display_name})</option>
              ))}
            </select>
          </label>
          <label className="text-rd-dim">
            symbol
            <input
              value={tfSymbol}
              onChange={(e) => setTfSymbol(e.target.value)}
              className="w-full bg-rd-bg2 border border-rd-border px-1 py-0.5 text-rd-text"
              data-testid="tf-symbol"
            />
          </label>
          <label className="text-rd-dim">
            lane
            <select
              value={tfLane}
              onChange={(e) => setTfLane(e.target.value)}
              className="w-full bg-rd-bg2 border border-rd-border px-1 py-0.5 text-rd-text"
              data-testid="tf-lane"
            >
              <option value="equity">equity</option>
              <option value="crypto">crypto</option>
            </select>
          </label>
          <label className="text-rd-dim">
            confidence
            <input
              type="number" step="0.01" min="0" max="1"
              value={tfConf}
              onChange={(e) => setTfConf(e.target.value)}
              className="w-full bg-rd-bg2 border border-rd-border px-1 py-0.5 text-rd-text"
              data-testid="tf-conf"
            />
          </label>
          <label className="text-rd-dim">
            notional USD
            <input
              type="number" step="100" min="0"
              value={tfNotional}
              onChange={(e) => setTfNotional(e.target.value)}
              className="w-full bg-rd-bg2 border border-rd-border px-1 py-0.5 text-rd-text"
              data-testid="tf-notional"
            />
          </label>
          <label className="text-rd-dim">
            spread_bps (gov)
            <input
              type="number" step="0.1"
              value={tfSpread}
              onChange={(e) => setTfSpread(e.target.value)}
              placeholder="empty=no signal"
              className="w-full bg-rd-bg2 border border-rd-border px-1 py-0.5 text-rd-text"
              data-testid="tf-spread"
            />
          </label>
          <label className="text-rd-dim">
            rvol (gov)
            <input
              type="number" step="0.1"
              value={tfRvol}
              onChange={(e) => setTfRvol(e.target.value)}
              placeholder="empty=no signal"
              className="w-full bg-rd-bg2 border border-rd-border px-1 py-0.5 text-rd-text"
              data-testid="tf-rvol"
            />
          </label>
        </div>
        <label className="flex items-center gap-1 mt-2 font-mono text-[10px] text-rd-dim">
          <input
            type="checkbox"
            checked={tfEarnings}
            onChange={(e) => setTfEarnings(e.target.checked)}
            data-testid="tf-earnings"
          />
          earnings_within_window (forces vote_required)
        </label>
        <button
          onClick={fireTest}
          disabled={busy}
          className="mt-2 px-3 py-1 border-2 border-rd-accent text-rd-accent font-mono text-[10px] uppercase tracking-wider disabled:opacity-40 hover:bg-rd-accent/10"
          data-testid="tf-fire"
        >
          {busy ? "…" : "FIRE EVALUATE"}
        </button>
        {tfResult && (
          <div className="mt-2 border border-rd-border bg-rd-bg2 p-2 font-mono text-[10px]" data-testid="tf-result">
            <div className={
              tfResult.decision === "EXECUTED" ? "text-rd-success font-bold" :
              tfResult.decision === "PENDING_VOTE" ? "text-rd-warn font-bold" :
              "text-rd-danger font-bold"
            }>
              {tfResult.decision}
              {tfResult.final_notional_usd !== null && tfResult.final_notional_usd !== undefined && (
                <> · ${tfResult.final_notional_usd}</>
              )}
            </div>
            <div className="text-rd-dim mt-1">{tfResult.reason}</div>
            {tfResult.pipeline_trace?.governor?.applied_rules?.length > 0 && (
              <div className="mt-1 text-rd-dim">
                governor applied:&nbsp;
                {tfResult.pipeline_trace.governor.applied_rules.map((r) => r.rule_id).join(", ")}
              </div>
            )}
          </div>
        )}
      </details>

      {/* Recent evaluations feed */}
      <details className="font-mono text-[10px]" data-testid="paradox-v2-feed">
        <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
          Recent /v2/evaluate receipts ({data.recent_evaluations.length}) ▾
        </summary>
        <div className="mt-1 space-y-0.5 max-h-60 overflow-auto">
          {data.recent_evaluations.length === 0 && (
            <div className="text-rd-dim italic">no evaluations yet — fire a test above</div>
          )}
          {data.recent_evaluations.map((r) => (
            <div key={r.evaluation_id} className={
              "flex items-center gap-2 border-l-2 pl-2 py-0.5 " +
              (r.decision === "EXECUTED" ? "border-rd-success" :
               r.decision === "PENDING_VOTE" ? "border-rd-warn" :
               "border-rd-danger")
            }>
              <span className={
                r.decision === "EXECUTED" ? "text-rd-success font-bold" :
                r.decision === "PENDING_VOTE" ? "text-rd-warn font-bold" :
                "text-rd-danger font-bold"
              }>
                {r.decision}
              </span>
              <span className="text-rd-text">{r.opinion?.symbol}</span>
              <span className="text-rd-dim">{r.seat_id}</span>
              <span className="text-rd-dim">{r.opinion?.brain_id}</span>
              {r.final_notional_usd !== null && r.final_notional_usd !== undefined && (
                <span className="text-rd-dim">${r.final_notional_usd}</span>
              )}
              <span className="text-rd-dim ml-auto truncate max-w-[200px]" title={r.reason}>{r.reason}</span>
              <span className="text-rd-dim">{r.ts?.slice(11, 19)}</span>
            </div>
          ))}
        </div>
      </details>

      {/* Promotion log */}
      <details className="font-mono text-[10px]" data-testid="paradox-v2-promotions">
        <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
          Autonomy promotion log ({data.promotion_log.length}) ▾
        </summary>
        <div className="mt-1 space-y-0.5">
          {data.promotion_log.length === 0 && (
            <div className="text-rd-dim italic">no autonomy changes yet</div>
          )}
          {data.promotion_log.map((p) => (
            <div key={p.promotion_id} className="border-l-2 border-rd-accent pl-2">
              <span className="text-rd-text">{p.seat_id}</span>
              <span className="text-rd-dim">: {p.from_mode} → </span>
              <span className="text-rd-text font-bold">{p.to_mode}</span>
              <span className="text-rd-dim"> by {p.triggered_by}</span>
              <div className="text-rd-dim text-[9px]">{p.reason}</div>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

// ─── Promotion Readiness Strip ──────────────────────────────────────
//
// Operator-driven promotion gate. Polls /v2/seats/pilot-readiness.
// Surfaces per-seat decision-quality stats (eval count, BLOCKED vs
// REJECTED breakdown, avg confidence) since the LAST promotion to
// the seat's current autonomy_mode. When eval_count crosses the
// threshold (default 25), the row turns green and the "promote to
// {next_mode}" button activates. No verifier auto-promotion — the
// operator is the only path. See routes/paradox_v2.py for the
// threshold env override (PARADOX_V2_PILOT_PROMOTION_MIN_EVALS).

function PromotionReadinessStrip({ onPromote, busy }) {
  const [state, setState] = React.useState({ readiness: [], threshold: 25, loading: true, err: null });

  const load = React.useCallback(async () => {
    try {
      const r = await api.get("/v2/seats/pilot-readiness");
      setState({
        readiness: r.data.readiness || [],
        threshold: r.data.threshold || 25,
        loading: false,
        err: null,
      });
    } catch (e) {
      setState((s) => ({ ...s, loading: false, err: e }));
    }
  }, []);

  React.useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (!alive) return;
      await load();
      if (alive) setTimeout(tick, 15000);
    };
    tick();
    return () => { alive = false; };
  }, [load]);

  if (state.loading && state.readiness.length === 0) {
    return (
      <div className="border border-rd-border bg-rd-bg p-2 font-mono text-[10px] text-rd-dim" data-testid="paradox-v2-readiness-loading">
        loading promotion readiness …
      </div>
    );
  }
  if (state.err) {
    return (
      <div className="border border-rd-danger/50 bg-rd-danger/5 p-2 font-mono text-[10px] text-rd-danger" data-testid="paradox-v2-readiness-error">
        promotion readiness failed: {String(state.err.message || state.err)}
      </div>
    );
  }

  const promotable = state.readiness.filter((r) => r.promotable && r.next_mode);
  const inProgress = state.readiness.filter((r) => !r.promotable && r.next_mode);

  const tryPromote = async (row) => {
    if (!row.next_mode) return;
    const reason = window.prompt(
      `Promote ${row.seat_id} from ${row.current_mode} → ${row.next_mode}?\nType a short audit reason (≥ 4 chars):`,
      `clean ${row.eval_count}-eval observe window`,
    );
    if (!reason || reason.trim().length < 4) return;
    await onPromote(row.seat_id, row.next_mode);
    await load();
  };

  return (
    <div className="border border-rd-border bg-rd-bg p-2 font-mono text-[10px] space-y-1" data-testid="paradox-v2-promotion-readiness">
      <div className="flex items-baseline justify-between">
        <span className="text-[9px] uppercase text-rd-dim tracking-wider">
          Promotion Readiness · {state.threshold}-eval gate
        </span>
        <span className="text-[9px] text-rd-dim">operator-driven · no auto-promote</span>
      </div>

      {/* Promotable section (green, on top) */}
      {promotable.length > 0 && (
        <div className="space-y-1">
          {promotable.map((r) => (
            <div
              key={r.seat_id}
              className="flex items-center justify-between border border-rd-success/40 bg-rd-success/5 px-2 py-1"
              data-testid={`readiness-row-${r.seat_id}`}
            >
              <div className="flex items-baseline gap-2">
                <span className="text-rd-success font-bold">{r.seat_id}</span>
                <span className="text-[9px] text-rd-dim uppercase">{r.instrument_type}</span>
                <span className="text-rd-text">
                  {r.eval_count}/{state.threshold} evals
                </span>
                <span className="text-rd-dim">
                  · {r.blocked_count} BLOCKED · {r.rejected_seat_count} REJ_SEAT
                  {r.rejected_roadguard_count > 0 && (
                    <span className="text-rd-danger"> · {r.rejected_roadguard_count} REJ_RG</span>
                  )}
                </span>
                {typeof r.avg_confidence === "number" && (
                  <span className="text-rd-dim">· avg conf {r.avg_confidence}</span>
                )}
              </div>
              <button
                disabled={busy}
                onClick={() => tryPromote(r)}
                className="text-rd-success font-bold hover:underline"
                data-testid={`promote-ready-${r.seat_id}`}
              >
                READY → promote to {r.next_mode}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* In-progress section (dim, below) */}
      {inProgress.length > 0 && (
        <div className="space-y-0.5 pt-1 border-t border-rd-border/40">
          {inProgress.map((r) => {
            const pct = Math.min(100, (r.eval_count / state.threshold) * 100);
            const stalled = r.rejected_roadguard_count > 0;
            return (
              <div
                key={r.seat_id}
                className="flex items-center justify-between px-2 py-0.5"
                data-testid={`readiness-row-${r.seat_id}`}
              >
                <div className="flex items-baseline gap-2 flex-1">
                  <span className="text-rd-text">{r.seat_id}</span>
                  <span className="text-[9px] text-rd-dim uppercase">{r.instrument_type}</span>
                  <span className="text-rd-dim">
                    {r.current_mode}{r.next_mode ? ` → ${r.next_mode}` : ""}
                  </span>
                  <span className="text-rd-dim">
                    {r.eval_count}/{state.threshold} evals
                  </span>
                  {stalled && (
                    <span className="text-rd-danger" title="RoadGuard fired in this window — clear the underlying issue before promotion">
                      ⚠ RG-stalled
                    </span>
                  )}
                </div>
                <div className="w-24 h-1 bg-rd-border relative">
                  <div
                    className={stalled ? "h-full bg-rd-danger" : "h-full bg-rd-accent"}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {promotable.length === 0 && inProgress.length === 0 && (
        <div className="text-rd-dim italic">no seats in a promotable mode (all at auto_execute or no policy rows)</div>
      )}
    </div>
  );
}
