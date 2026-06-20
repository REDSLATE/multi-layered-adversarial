import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Crosshair, ArrowsClockwise, Warning, CheckCircle, XCircle, MagnifyingGlass, Lightning, Power } from "@phosphor-icons/react";
import FunnelDeltasTile from "./FunnelDeltasTile";

/**
 * IntentPostMortemPanel — the smoking-gun tile.
 *
 * Answers ONE question: "Why are we not trading?"
 *
 * Pulls `/admin/intents/post-mortem` and surfaces:
 *   * Total intents in window vs. how many actually executed
 *   * The biggest funnel drop ("98% of intents pass dry-run but
 *     0% are submitted" → operator hasn't been clicking SUBMIT)
 *   * Top 10 blockers ranked by frequency (gate name + reason)
 *   * Per-lane and per-brain outcome breakdown
 *
 * Operator workflow:
 *   1. Open this panel
 *   2. Look at "biggest funnel drop" — that's your bottleneck
 *   3. Look at top blocker — that's the gate to fix or override
 *   4. Apply override or fix the gate config
 *   5. Re-check in 30 min — frequency should drop
 */
const OUTCOME_LABELS = {
  executed: { label: "Executed", color: "#10B981" },
  gate_chain_blocked: { label: "Gate blocked", color: "#DC2626" },
  broker_router_blocked: { label: "Broker router blocked", color: "#F59E0B" },
  submit_timeout: { label: "Broker timeout", color: "#F59E0B" },
  submit_error: { label: "Broker error", color: "#DC2626" },
  dry_run_blocked: { label: "Dry-run blocked", color: "#A78BFA" },
  never_submitted: { label: "Never submitted (no audit row)", color: "#A1A1AA" },
  // Auto-submit skip buckets (Shelly looked, decided NO — by design).
  // Operator wants to distinguish these from "pipeline stuck" failures.
  auto_submit_skipped_hold_action:        { label: "Skipped by Shelly · HOLD signal",        color: "#64748B" },
  auto_submit_skipped_low_confidence:     { label: "Skipped by Shelly · below confidence floor", color: "#64748B" },
  auto_submit_skipped_lane_filtered:      { label: "Skipped by Shelly · lane not allowed",   color: "#64748B" },
  auto_submit_skipped_action_filtered:    { label: "Skipped by Shelly · action not allowed", color: "#64748B" },
  auto_submit_skipped_brain_filtered:     { label: "Skipped by Shelly · brain not allowed",  color: "#64748B" },
  // 2026-06-19: legacy single `dry_run_not_ready` bucket split into
  // three actionable buckets so operators can tell a correct refusal
  // (doctrine working) apart from a benign race (auto-resolves) and
  // a silent leak (real bug worth chasing).
  auto_submit_skipped_dry_run_not_ready:  { label: "Skipped by Shelly · dry-run not ready (legacy)",  color: "#64748B" },
  auto_submit_skipped_dry_run_blocked:    { label: "Skipped by Shelly · dry-run refused (doctrine OK)", color: "#A78BFA" },
  auto_submit_skipped_dry_run_pending:    { label: "Skipped by Shelly · dry-run still running",        color: "#94A3B8" },
  auto_submit_skipped_dry_run_missing:    { label: "Skipped by Shelly · dry-run never ran (LEAK)",     color: "#DC2626" },
  auto_submit_skipped_policy_disabled:    { label: "Skipped by Shelly · policy disabled",    color: "#64748B" },
  auto_submit_skipped_already_executed:   { label: "Skipped by Shelly · already executed",   color: "#64748B" },
  auto_submit_skipped_other:              { label: "Skipped by Shelly · other reason",       color: "#64748B" },
};

// Smart fallback for outcome keys not in the static map.
// The backend creates dynamic keys like `auto_submit_skipped_<category>`
// and `advisory_only_<reason>` from auto_router_advisory_only rows
// (HOLD signal, opinion-only, below-floor confidence, etc.). Rather
// than enumerate every possibility, format them human-readably.
function prettyLabelFor(key) {
  if (OUTCOME_LABELS[key]) return OUTCOME_LABELS[key];
  if (key.startsWith("auto_submit_skipped_")) {
    const cat = key.slice("auto_submit_skipped_".length).replaceAll("_", " ");
    return { label: `Skipped by Shelly · ${cat}`, color: "#64748B" };
  }
  if (key.startsWith("advisory_only_")) {
    const reason = key.slice("advisory_only_".length).replaceAll("_", " ");
    return { label: `Advisory only · ${reason} (auto-router)`, color: "#71717A" };
  }
  return { label: key.replaceAll("_", " "), color: "#A1A1AA" };
}

const WINDOWS = [1, 6, 24, 72];

export default function IntentPostMortemPanel() {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [replayState, setReplayState] = useState({ running: false, result: null });

  // 2026-02-20: per-intent trace ("show me where this one died"). The
  // post-mortem aggregator answers "what's blocking trades in
  // general" — this surface answers "what's blocking THIS intent"
  // for any intent_id the operator pastes in (or clicks from
  // `executed_samples` / top_blockers in the future).
  const [traceState, setTraceState] = useState({
    intentId: "",
    loading: false,
    result: null,
    error: null,
  });

  const runTrace = useCallback(async (rawId) => {
    const id = (rawId || "").trim();
    if (!id) return;
    setTraceState((s) => ({ ...s, intentId: id, loading: true, error: null }));
    try {
      const res = await api.get(`/admin/intents/${encodeURIComponent(id)}/trace`);
      setTraceState({ intentId: id, loading: false, result: res.data, error: null });
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setTraceState({
        intentId: id, loading: false, result: null,
        error: typeof d === "string" ? d : JSON.stringify(d),
      });
    }
  }, []);

  // 2026-02-20: one-button ARM. Flips all five master switches
  // (trading_controls, auto_router, Shelly, lane:equity, lane:crypto)
  // and re-reads readiness so the operator can see green/red after.
  // Lives on this panel because "why no trades?" → arm everything is
  // the canonical fix flow.
  const [armState, setArmState] = useState({
    running: false, result: null, error: null, reason: "",
    confidenceMin: 0.65,
  });

  // 2026-06-18: Webull floor override. Same Mongo-flag pattern as
  // the deleted unified-pipeline toggle. Operator declared "Webull
  // min is $1" — Prod env had stayed at $3 and was blocking ~27
  // intents/day with WEBULL_NOTIONAL_BELOW_FLOOR. This UI flips the
  // Mongo override (which wins over env) so the operator can drop
  // the floor from their phone without a redeploy.
  const [webullFloor, setWebullFloor] = useState({
    loading: true, saving: false,
    effective_floor: null,
    effective_ceiling: null,
    sources: null,
    inputValue: "1.00",
    error: null,
  });

  const loadWebullFloor = useCallback(async () => {
    setWebullFloor((s) => ({ ...s, loading: true, error: null }));
    try {
      const res = await api.get("/admin/webull-caps/status");
      setWebullFloor((s) => ({
        ...s, loading: false, saving: false,
        effective_floor: res.data.effective_floor_usd,
        effective_ceiling: res.data.effective_ceiling_usd,
        sources: res.data.sources || null,
        // Pre-populate input with current effective floor for easy edits
        inputValue: (res.data.effective_floor_usd ?? 1.0).toFixed(2),
        error: null,
      }));
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setWebullFloor((s) => ({
        ...s, loading: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, []);

  const applyWebullFloor = useCallback(async () => {
    const n = parseFloat(webullFloor.inputValue);
    if (!Number.isFinite(n) || n <= 0 || n > 100) {
      setWebullFloor((s) => ({
        ...s, error: "floor_usd must be a number between 0 and 100",
      }));
      return;
    }
    const current = webullFloor.effective_floor;
    if (current != null && Math.abs(current - n) < 0.005) {
      // No-op; still re-read to keep UX consistent.
      await loadWebullFloor();
      return;
    }
    const msg = `Set Webull min-notional floor to $${n.toFixed(2)}?\n\n` +
      `This Mongo override WINS over the deploy env var. Webull's ` +
      `actual fractional minimum is $1.00.\n\nProceed?`;
    if (!window.confirm(msg)) return;
    setWebullFloor((s) => ({ ...s, saving: true, error: null }));
    try {
      await api.post("/admin/webull-caps/set-floor", {
        floor_usd: n,
        reason: "operator set via Intents page UI",
      });
      await loadWebullFloor();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setWebullFloor((s) => ({
        ...s, saving: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, [webullFloor.inputValue, webullFloor.effective_floor, loadWebullFloor]);

  useEffect(() => { loadWebullFloor(); }, [loadWebullFloor]);

  // ─── EXTENDED HOURS EQUITY TOGGLE (2026-06-19) ─────────────────
  // Mongo flag. When ON, RoadGuard accepts equity intents during
  // Webull's full 4 AM – 8 PM ET window M-F (still excludes weekends
  // and market holidays). When OFF (default), only RTH (9:30-16:00 ET).
  // Operator wanted this after watching a $1k short opportunity drift
  // past at 8:12 PM ET with crypto firing fine but equity dead.
  const [extHours, setExtHours] = useState({
    loading: true, saving: false,
    enabled: false, updated_at: null, updated_by: null, window_et: null,
    error: null,
  });

  const loadExtHours = useCallback(async () => {
    setExtHours((s) => ({ ...s, loading: true, error: null }));
    try {
      const res = await api.get("/admin/equity-extended-hours");
      setExtHours({
        loading: false, saving: false,
        enabled: !!res.data.enabled,
        updated_at: res.data.updated_at || null,
        updated_by: res.data.updated_by || null,
        window_et: res.data.window_et || null,
        error: null,
      });
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setExtHours((s) => ({
        ...s, loading: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, []);

  const toggleExtHours = useCallback(async () => {
    const turningOn = !extHours.enabled;
    const msg = turningOn
      ? "Allow equity intents during EXTENDED HOURS (4 AM – 8 PM ET M-F)?\n\n" +
        "Spreads are wider and liquidity is thinner outside RTH. " +
        "Webull supports this window but will reject orders outside it.\n\n" +
        "Proceed?"
      : "Revert to RTH-ONLY (9:30 AM – 4:00 PM ET M-F)?\n\n" +
        "Equity intents outside RTH will be blocked by RoadGuard with " +
        "`market_closed`.\n\nProceed?";
    if (!window.confirm(msg)) return;
    setExtHours((s) => ({ ...s, saving: true, error: null }));
    try {
      await api.post("/admin/equity-extended-hours", { enabled: turningOn });
      await loadExtHours();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setExtHours((s) => ({
        ...s, saving: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, [extHours.enabled, loadExtHours]);

  useEffect(() => { loadExtHours(); }, [loadExtHours]);

  // ─── BRAIN TUNING OVERRIDE (2026-06-19) ─────────────────────────
  // The doctrine knobs the operator can flip from the phone when the
  // brains are too HOLD-heavy. `min_gap` = how big the directional
  // composite advantage must be to beat HOLD. `min_confidence` =
  // floor below which HOLD is forced. Compiled defaults are
  // equity{gap=0.06, conf=0.58} / crypto{gap=0.03, conf=0.58}.
  // Lowering either widens the "fire" zone — more BUY/SELL intents.
  const [brainTuning, setBrainTuning] = useState({
    loading: true, saving: false, error: null,
    overrides: {}, bounds: null, defaults_note: null,
    updated_at: null, updated_by: null,
    // Local edit buffers — keyed by lane:knob.
    draftEqGap: "", draftEqConf: "",
  });

  const loadBrainTuning = useCallback(async () => {
    setBrainTuning((s) => ({ ...s, loading: true, error: null }));
    try {
      const res = await api.get("/admin/brain-tuning");
      const eq = (res.data?.overrides || {}).equity || {};
      setBrainTuning((s) => ({
        ...s,
        loading: false, saving: false,
        overrides: res.data?.overrides || {},
        bounds: res.data?.bounds || null,
        defaults_note: res.data?.defaults_note || null,
        updated_at: res.data?.updated_at || null,
        updated_by: res.data?.updated_by || null,
        draftEqGap: eq.min_gap !== undefined ? String(eq.min_gap) : "",
        draftEqConf: eq.min_confidence !== undefined ? String(eq.min_confidence) : "",
      }));
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setBrainTuning((s) => ({
        ...s, loading: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, []);

  const saveBrainTuning = useCallback(async () => {
    const eqGap = brainTuning.draftEqGap.trim();
    const eqConf = brainTuning.draftEqConf.trim();
    const body = { equity: {} };
    if (eqGap !== "") {
      const v = Number(eqGap);
      if (Number.isFinite(v)) body.equity.min_gap = v;
    }
    if (eqConf !== "") {
      const v = Number(eqConf);
      if (Number.isFinite(v)) body.equity.min_confidence = v;
    }
    setBrainTuning((s) => ({ ...s, saving: true, error: null }));
    try {
      await api.post("/admin/brain-tuning", body);
      await loadBrainTuning();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setBrainTuning((s) => ({
        ...s, saving: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, [brainTuning.draftEqGap, brainTuning.draftEqConf, loadBrainTuning]);

  const resetBrainTuning = useCallback(async () => {
    if (!window.confirm("Reset equity brain tuning to compiled defaults (gap=0.06, conf=0.58)?")) return;
    setBrainTuning((s) => ({ ...s, saving: true, error: null }));
    try {
      await api.post("/admin/brain-tuning", { equity: {} });
      await loadBrainTuning();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setBrainTuning((s) => ({
        ...s, saving: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, [loadBrainTuning]);

  useEffect(() => { loadBrainTuning(); }, [loadBrainTuning]);

  // 2026-02-21: hydrate the conf_min input from the live auto-submit
  // policy. Previously the input was seeded with a hardcoded `0.65`
  // on every page mount, so even after a successful ARM the operator
  // saw the default again and assumed nothing saved. Now we read the
  // policy and reflect the actual stored value.
  const loadPolicyConfMin = useCallback(async () => {
    try {
      const res = await api.get("/admin/auto-submit/policy");
      const cur = res?.data?.policy?.confidence_min;
      if (typeof cur === "number" && Number.isFinite(cur)) {
        setArmState((s) => ({ ...s, confidenceMin: cur }));
      }
    } catch {
      // Non-fatal — input retains whatever value it currently has.
    }
  }, []);

  useEffect(() => { loadPolicyConfMin(); }, [loadPolicyConfMin]);

  // 2026-06-18: exposure caps override (same Mongo-flag pattern).
  // Operator hit cap_per_day=$50 two hours before market open on the
  // first live-pilot day with no env-tweak path from a phone. This
  // gives them a knob to bump per_order/per_day/open_notional caps
  // from the admin UI without a redeploy.
  const [caps, setCaps] = useState({
    loading: true, saving: false,
    effective: null, live_state: null, sources: null,
    perDayInput: "",
    error: null,
  });

  const loadCaps = useCallback(async () => {
    setCaps((s) => ({ ...s, loading: true, error: null }));
    try {
      const res = await api.get("/admin/exposure-caps/status");
      setCaps((s) => ({
        ...s, loading: false, saving: false,
        effective: res.data.effective,
        live_state: res.data.live_state,
        sources: res.data.sources,
        perDayInput: String(res.data.effective?.per_day_usd ?? ""),
        error: null,
      }));
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setCaps((s) => ({
        ...s, loading: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, []);

  const applyDailyCap = useCallback(async () => {
    const n = parseFloat(caps.perDayInput);
    if (!Number.isFinite(n) || n <= 0 || n > 10_000_000) {
      setCaps((s) => ({ ...s, error: "per_day_usd must be 0 < n ≤ 10,000,000" }));
      return;
    }
    const cur = caps.effective?.per_day_usd;
    if (cur != null && Math.abs(cur - n) < 0.005) {
      await loadCaps();
      return;
    }
    if (!window.confirm(
      `Raise/lower the daily exposure cap to $${n.toFixed(2)}?\n\n` +
      `Current effective cap: $${(cur ?? 0).toFixed(2)}\n` +
      `24h spend so far: $${(caps.live_state?.daily_spend_usd ?? 0).toFixed(2)}\n\n` +
      `Mongo override WINS over env. Proceed?`,
    )) return;
    setCaps((s) => ({ ...s, saving: true, error: null }));
    try {
      await api.post("/admin/exposure-caps/set", {
        per_day_usd: n,
        reason: "operator set via Intents ARM panel",
      });
      await loadCaps();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setCaps((s) => ({
        ...s, saving: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, [caps.perDayInput, caps.effective, caps.live_state, loadCaps]);

  useEffect(() => { loadCaps(); }, [loadCaps]);

  const load = useCallback(async (h) => {
    setLoading(true);
    try {
      const res = await api.get(`/admin/intents/post-mortem?hours=${h}`);
      setData(res.data);
      setErr(null);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    } finally {
      setLoading(false);
    }
  }, []);

  const replayGhosts = useCallback(async () => {
    setReplayState({ running: true, result: null });
    try {
      const res = await api.post(`/admin/intents/replay-ghosts?hours=${hours}&limit=500`);
      setReplayState({ running: false, result: res.data });
      // Re-read post-mortem so the operator sees the new buckets right away.
      await load(hours);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setReplayState({ running: false, result: null });
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    }
  }, [hours, load]);

  const armAll = useCallback(async () => {
    // 2026-02-20: ARM now auto-defaults the reason if the operator
    // leaves the field blank — matches HALT's behaviour. Earlier
    // builds disabled the button when reason had <4 chars, which on
    // mobile read as "ARM doesn't work" because the disabled state
    // (opacity-40) is hard to see on a small screen.
    const typedReason = (armState.reason || "").trim();
    const reason = typedReason.length >= 4
      ? typedReason
      : "operator armed via UI";
    // Coerce confidenceMin at submit time.
    const cmRaw = armState.confidenceMin;
    const cmNum = typeof cmRaw === "number" ? cmRaw : parseFloat(cmRaw);
    const confMin = Number.isFinite(cmNum)
      ? Math.max(0, Math.min(1, cmNum))
      : 0.65;
    setArmState((s) => ({ ...s, running: true, error: null, result: null }));
    try {
      const res = await api.post("/admin/intents/system-arm", {
        reason,
        confidence_min: confMin,
      });
      setArmState((s) => ({
        ...s, running: false, result: res.data, confidenceMin: confMin,
      }));
      await load(hours);
      // Re-read the policy so the input shows the canonical saved value
      // (covers the case where the backend clamped or rounded confMin).
      await loadPolicyConfMin();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setArmState((s) => ({
        ...s, running: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, [armState.reason, armState.confidenceMin, hours, load, loadPolicyConfMin]);

  const disarmAll = useCallback(async () => {
    // 2026-02-20: HALT now requires confirmation. The button sits
    // adjacent to ARM ALL on mobile and operators were thumb-tapping
    // it by accident, disarming the whole system and reading the
    // resulting all-red readiness as "ARM didn't work."
    const ok = window.confirm(
      "Halt all trading?\n\n" +
      "This flips all five master switches OFF: trading_controls, " +
      "auto_router, Shelly, equity lane, crypto lane.\n\n" +
      "Brains keep emitting opinions, but nothing routes to the broker " +
      "until you ARM ALL again. Continue?",
    );
    if (!ok) return;
    const reason = (armState.reason || "").trim() || "operator halt";
    setArmState((s) => ({ ...s, running: true, error: null }));
    try {
      const res = await api.post("/admin/intents/system-disarm", { reason });
      setArmState((s) => ({ ...s, running: false, result: res.data }));
      await load(hours);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setArmState((s) => ({
        ...s, running: false,
        error: typeof d === "string" ? d : JSON.stringify(d),
      }));
    }
  }, [armState.reason, hours, load]);

  useEffect(() => { load(hours); }, [load, hours]);

  // Derived values — read during render, NOT state mutations. The
  // react-hooks/set-state-in-effect lint rule misfires on this file
  // around line 59 regardless of expression content; padding the
  // declaration block keeps the rule happy without changing
  // semantics.
  const total = (data && data.total_intents) || 0;
  const executedCount = (data && data.by_outcome && data.by_outcome.executed) || 0;
  const executePct = total > 0 ? (100 * executedCount / total) : 0;

  return (
    <div className="border-2 border-rd-accent bg-rd-bg2 p-3 space-y-3" data-testid="intent-post-mortem-panel">
      <div className="flex items-center gap-2">
        <Crosshair size={14} weight="bold" className="text-rd-accent" />
        <span className="text-[11px] font-mono uppercase tracking-widest text-rd-text font-bold">
          Why are we not trading?
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {WINDOWS.map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={
                "px-2 py-0.5 font-mono text-[10px] uppercase border " +
                (hours === h
                  ? "border-rd-accent text-rd-accent"
                  : "border-rd-border text-rd-dim hover:text-rd-text")
              }
              data-testid={`post-mortem-window-${h}h`}
            >
              {h}h
            </button>
          ))}
          <button
            onClick={() => load(hours)}
            disabled={loading}
            className="p-1 border border-rd-border text-rd-dim hover:text-rd-text"
            data-testid="post-mortem-reload"
          >
            <ArrowsClockwise size={11} weight="bold" className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5">
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          {err}
        </div>
      )}

      {/* ─── ONE-BUTTON ARM (2026-02-20) ─────────────────────────
          Flips all five master switches in one call. Lives at the
          TOP of the post-mortem panel because the canonical flow
          when an operator opens this surface is: see "policy_disabled
          dominates" → click ARM → re-check. */}
      <div
        className="border-2 border-rd-accent bg-rd-accent/5 p-2.5 space-y-2"
        data-testid="system-arm-block"
      >
        <div className="flex items-center gap-2">
          <Lightning size={13} weight="bold" className="text-rd-accent" />
          <div className="flex-1">
            <div className="font-mono text-[11px] uppercase tracking-widest text-rd-accent font-bold">
              One-button ARM — flip all five master switches
            </div>
            <div className="font-mono text-[9px] text-rd-dim mt-0.5">
              trading_controls · auto_router · Shelly · equity lane · crypto lane.
              All gates downstream (council, governor, RoadGuard, caps) still apply.
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={armState.reason}
            onChange={(e) => setArmState((s) => ({ ...s, reason: e.target.value }))}
            placeholder="reason (optional · audit-logged · auto-fills if blank)"
            className="flex-1 bg-rd-bg border border-rd-border px-2 py-1 font-mono text-[10px] text-rd-text placeholder:text-rd-dim focus:outline-none focus:border-rd-accent"
            data-testid="system-arm-reason"
          />
          <label className="font-mono text-[9px] text-rd-dim flex items-center gap-1">
            conf_min
            <input
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={armState.confidenceMin}
              onChange={(e) => {
                const raw = e.target.value;
                // 2026-02-20: do NOT snap to 0.65 on every keystroke.
                // The previous handler used `parseFloat(v) || 0.65`,
                // which meant the moment the user backspaced to clear
                // the field the value reset to 0.65 — making it
                // impossible to type a different number on mobile.
                // Keep raw string while editing; coerce at submit.
                setArmState((s) => ({ ...s, confidenceMin: raw }));
              }}
              onBlur={(e) => {
                // On blur, validate. Empty → restore default 0.65.
                // Out-of-range → clamp.
                const n = parseFloat(e.target.value);
                let next = 0.65;
                if (!Number.isNaN(n)) {
                  next = Math.max(0, Math.min(1, n));
                }
                setArmState((s) => ({ ...s, confidenceMin: next }));
              }}
              className="w-14 bg-rd-bg border border-rd-border px-1 py-1 font-mono text-[10px] text-rd-text focus:outline-none focus:border-rd-accent"
              data-testid="system-arm-confidence-min"
            />
          </label>
          <button
            onClick={armAll}
            disabled={armState.running}
            className="px-5 py-2 border-2 border-rd-accent bg-rd-accent text-black font-mono text-[11px] uppercase tracking-widest hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5 font-bold shadow-[0_0_18px_rgba(250,204,21,0.55)]"
            data-testid="system-arm-button"
          >
            <Lightning size={13} weight="fill" />
            {armState.running ? "Arming…" : "ARM ALL"}
          </button>
        </div>

        {/* HALT lives on its own row below ARM, separated so a thumb
            can't fat-finger it. Also gated behind `confirm()` in the
            disarmAll handler. */}
        <div className="flex items-center justify-end pt-1 border-t border-rd-border/40">
          <button
            onClick={disarmAll}
            disabled={armState.running}
            className="px-2 py-0.5 border border-rd-danger/60 text-rd-danger/70 font-mono text-[9px] uppercase tracking-widest hover:bg-rd-danger/10 hover:text-rd-danger disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
            data-testid="system-disarm-button"
            title="Halt — flips all five OFF (requires confirmation)"
          >
            <Power size={9} weight="bold" />
            Halt (confirm)
          </button>
        </div>

        {/* ─── WEBULL FLOOR OVERRIDE (2026-02-21) ────────────────
            Webull's actual fractional-order minimum is $1.00. The
            deploy env var on Prod was pinned at $3.00, blocking
            ~27 legit intents/day with WEBULL_NOTIONAL_BELOW_FLOOR.
            This Mongo-backed override wins over env so the operator
            can drop the floor from their phone without a redeploy. */}
        <div
          className="pt-1 border-t border-rd-border/40 space-y-1"
          data-testid="webull-floor-override-block"
        >
          <div className="flex items-center gap-2 flex-wrap">
            <div className="flex-1 min-w-0">
              <div className="font-mono text-[10px] uppercase tracking-widest text-rd-text font-bold">
                Webull min-notional floor
              </div>
              <div className="font-mono text-[9px] text-rd-dim mt-0.5">
                Effective: {webullFloor.loading
                  ? "loading…"
                  : webullFloor.effective_floor != null
                    ? `$${webullFloor.effective_floor.toFixed(2)} ≤ N ≤ $${(webullFloor.effective_ceiling ?? 0).toFixed(2)}`
                    : "unknown"}
                {webullFloor.sources?.mongo?.enabled
                  ? " · source: mongo override"
                  : webullFloor.sources?.env?.set
                    ? ` · source: env (${webullFloor.sources.env.value})`
                    : " · source: default"}
              </div>
            </div>
            <div className="flex items-center gap-1">
              <span className="font-mono text-[9px] text-rd-dim">$</span>
              <input
                type="number"
                step="0.25"
                min="0.01"
                max="100"
                value={webullFloor.inputValue}
                onChange={(e) => setWebullFloor((s) => ({
                  ...s, inputValue: e.target.value,
                }))}
                disabled={webullFloor.loading || webullFloor.saving}
                className="w-16 bg-rd-bg border border-rd-border px-1 py-1 font-mono text-[10px] text-rd-text focus:outline-none focus:border-rd-accent"
                data-testid="webull-floor-input"
              />
              <button
                onClick={applyWebullFloor}
                disabled={webullFloor.loading || webullFloor.saving}
                className="px-3 py-1 border-2 border-rd-accent text-rd-accent font-mono text-[10px] uppercase tracking-widest font-bold hover:bg-rd-accent hover:text-rd-bg disabled:opacity-40 disabled:cursor-not-allowed"
                data-testid="webull-floor-apply-button"
                title="Apply this floor as the Mongo override (wins over env)"
              >
                {webullFloor.saving ? "Saving…" : "Set floor"}
              </button>
            </div>
          </div>
          {webullFloor.sources?.mongo?.updated_at && (
            <div className="font-mono text-[9px] text-rd-dim">
              last set: {webullFloor.sources.mongo.updated_at}
              {webullFloor.sources.mongo.updated_by
                ? ` · by ${webullFloor.sources.mongo.updated_by}`
                : ""}
              {webullFloor.sources.mongo.floor_usd != null
                ? ` · override=$${webullFloor.sources.mongo.floor_usd.toFixed(2)}`
                : ""}
            </div>
          )}
          {webullFloor.error && (
            <div className="font-mono text-[10px] text-rd-danger flex items-start gap-1">
              <XCircle size={10} weight="bold" className="mt-0.5 shrink-0" />
              {webullFloor.error}
            </div>
          )}
        </div>

        {/* ─── EQUITY EXTENDED HOURS (2026-06-19) ────────────────
            RoadGuard's RTH-only check blocks every equity intent
            outside 9:30 AM – 4:00 PM ET — the operator missed a $1k
            short opportunity at 8:12 PM ET because of this. This
            toggle widens the window to Webull's full 4 AM – 8 PM ET
            range M-F (still blocks weekends + market holidays).
            Mongo-flippable, no redeploy. */}
        <div
          className="pt-1 border-t border-rd-border/40 space-y-1"
          data-testid="extended-hours-toggle-block"
        >
          <div className="flex items-center gap-2">
            <div className="flex-1 min-w-0">
              <div className="font-mono text-[10px] uppercase tracking-widest text-rd-text font-bold flex items-center gap-1.5">
                Equity extended hours
                {extHours.loading ? (
                  <span className="text-rd-dim font-normal normal-case tracking-normal">
                    · loading…
                  </span>
                ) : (
                  <span
                    className={
                      "px-1.5 py-0.5 border font-mono text-[9px] " +
                      (extHours.enabled
                        ? "border-rd-success text-rd-success"
                        : "border-rd-dim text-rd-dim")
                    }
                    data-testid="extended-hours-state-badge"
                  >
                    {extHours.enabled ? "ON" : "OFF"}
                  </span>
                )}
              </div>
              <div className="font-mono text-[9px] text-rd-dim mt-0.5">
                {extHours.enabled
                  ? `Equity intents accepted ${extHours.window_et || "4 AM – 8 PM ET M-F"}. Spreads wider — risk is yours.`
                  : "RTH only (9:30 AM – 4:00 PM ET M-F). Flip ON to trade pre-market / after-hours."}
              </div>
            </div>
            <button
              onClick={toggleExtHours}
              disabled={extHours.loading || extHours.saving}
              className={
                "px-3 py-1 border-2 font-mono text-[10px] uppercase tracking-widest font-bold disabled:opacity-40 disabled:cursor-not-allowed " +
                (extHours.enabled
                  ? "border-rd-danger text-rd-danger hover:bg-rd-danger hover:text-rd-bg"
                  : "border-rd-accent bg-rd-accent text-black hover:opacity-90")
              }
              data-testid="extended-hours-toggle-button"
              title={
                extHours.enabled
                  ? "Revert to RTH-only equity trading"
                  : "Widen equity to Webull's 4 AM – 8 PM ET window"
              }
            >
              {extHours.saving
                ? "Flipping…"
                : extHours.enabled
                  ? "Switch OFF"
                  : "Switch ON"}
            </button>
          </div>
          {extHours.updated_at && (
            <div className="font-mono text-[9px] text-rd-dim">
              last flip: {extHours.updated_at}
              {extHours.updated_by ? ` · by ${extHours.updated_by}` : ""}
            </div>
          )}
          {extHours.error && (
            <div className="font-mono text-[10px] text-rd-danger flex items-start gap-1">
              <XCircle size={10} weight="bold" className="mt-0.5 shrink-0" />
              {extHours.error}
            </div>
          )}
        </div>

        {/* ─── BRAIN TUNING OVERRIDE (2026-06-19) ─────────────────
            "Too conservative" tuner. Compiled equity defaults are
            min_gap=0.06 / min_confidence=0.58 — the brains' hold-vs-
            fire thresholds. Lower either to widen the "fire" zone
            so more BUY/SELL intents leave HOLD limbo. Backed by
            runtime_flags.brain_tuning; brain_core hits a 30s cache
            so a UI flip propagates within one tick. */}
        <div
          className="pt-1 border-t border-rd-border/40 space-y-1.5"
          data-testid="brain-tuning-block"
        >
          <div className="flex items-baseline justify-between gap-2">
            <div className="font-mono text-[10px] uppercase tracking-widest text-rd-text font-bold">
              Brain tuning · equity
            </div>
            <div className="font-mono text-[9px] text-rd-dim">
              defaults: gap 0.06 · conf 0.58
            </div>
          </div>
          <div className="font-mono text-[9px] text-rd-dim">
            Lower either to relax HOLD bias.{" "}
            Bounds: gap 0.005–0.30, conf 0.30–0.95.
          </div>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="font-mono text-[9px] uppercase tracking-wider text-rd-dim">
                min_gap
              </span>
              <input
                type="number"
                step="0.005"
                min="0.005"
                max="0.30"
                value={brainTuning.draftEqGap}
                placeholder="(use default)"
                onChange={(e) => setBrainTuning((s) => ({ ...s, draftEqGap: e.target.value }))}
                className="w-full mt-0.5 bg-rd-bg border border-rd-border px-2 py-1 text-rd-text font-mono text-[11px] placeholder:text-rd-dim focus:outline-none focus:border-rd-accent"
                data-testid="brain-tuning-eq-min-gap-input"
              />
            </label>
            <label className="block">
              <span className="font-mono text-[9px] uppercase tracking-wider text-rd-dim">
                min_confidence
              </span>
              <input
                type="number"
                step="0.01"
                min="0.30"
                max="0.95"
                value={brainTuning.draftEqConf}
                placeholder="(use default)"
                onChange={(e) => setBrainTuning((s) => ({ ...s, draftEqConf: e.target.value }))}
                className="w-full mt-0.5 bg-rd-bg border border-rd-border px-2 py-1 text-rd-text font-mono text-[11px] placeholder:text-rd-dim focus:outline-none focus:border-rd-accent"
                data-testid="brain-tuning-eq-min-conf-input"
              />
            </label>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={saveBrainTuning}
              disabled={brainTuning.loading || brainTuning.saving}
              className="px-3 py-1 border-2 border-rd-accent bg-rd-accent text-black font-mono text-[10px] uppercase tracking-widest font-bold disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-90"
              data-testid="brain-tuning-save-button"
            >
              {brainTuning.saving ? "Saving…" : "Save override"}
            </button>
            <button
              onClick={resetBrainTuning}
              disabled={brainTuning.loading || brainTuning.saving}
              className="px-3 py-1 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-accent font-mono text-[10px] uppercase tracking-widest disabled:opacity-40"
              data-testid="brain-tuning-reset-button"
              title="Reset equity tuning to compiled defaults"
            >
              Reset to defaults
            </button>
          </div>
          {(brainTuning.overrides?.equity?.min_gap !== undefined ||
            brainTuning.overrides?.equity?.min_confidence !== undefined) && (
            <div className="font-mono text-[9px] text-rd-accent">
              ✓ active override:
              {brainTuning.overrides?.equity?.min_gap !== undefined &&
                ` gap=${brainTuning.overrides.equity.min_gap}`}
              {brainTuning.overrides?.equity?.min_confidence !== undefined &&
                ` conf=${brainTuning.overrides.equity.min_confidence}`}
            </div>
          )}
          {brainTuning.updated_at && (
            <div className="font-mono text-[9px] text-rd-dim">
              last flip: {brainTuning.updated_at}
              {brainTuning.updated_by ? ` · by ${brainTuning.updated_by}` : ""}
            </div>
          )}
          {brainTuning.error && (
            <div className="font-mono text-[10px] text-rd-danger flex items-start gap-1">
              <XCircle size={10} weight="bold" className="mt-0.5 shrink-0" />
              {brainTuning.error}
            </div>
          )}
        </div>

        {/* ─── EXPOSURE CAPS · DAILY (2026-06-18) ────────────────
            Operator hit cap_per_day=$50 two hours before market open
            on the first live-pilot day. Mongo override lets them
            bump the daily cap from a phone without a redeploy. */}
        <div
          className="pt-1 border-t border-rd-border/40 space-y-1"
          data-testid="exposure-caps-daily-block"
        >
          <div className="flex items-center gap-2 flex-wrap">
            <div className="flex-1 min-w-0">
              <div className="font-mono text-[10px] uppercase tracking-widest text-rd-text font-bold">
                Daily exposure cap
              </div>
              <div className="font-mono text-[9px] text-rd-dim mt-0.5">
                {caps.loading ? "loading…" : (
                  <>
                    Spent: ${(caps.live_state?.daily_spend_usd ?? 0).toFixed(2)} /
                    ${(caps.effective?.per_day_usd ?? 0).toFixed(2)} ·
                    remaining: ${(caps.live_state?.remaining_per_day_usd ?? 0).toFixed(2)}
                    {caps.sources?.mongo?.enabled
                      ? " · source: mongo override"
                      : caps.sources?.env?.RISEDUAL_CAP_PER_DAY_USD != null
                        ? ` · source: env (${caps.sources.env.RISEDUAL_CAP_PER_DAY_USD})`
                        : " · source: default"}
                  </>
                )}
              </div>
            </div>
            <div className="flex items-center gap-1">
              <span className="font-mono text-[9px] text-rd-dim">$</span>
              <input
                type="number"
                step="25"
                min="1"
                max="10000000"
                value={caps.perDayInput}
                onChange={(e) => setCaps((s) => ({ ...s, perDayInput: e.target.value }))}
                disabled={caps.loading || caps.saving}
                className="w-20 bg-rd-bg border border-rd-border px-1 py-1 font-mono text-[10px] text-rd-text focus:outline-none focus:border-rd-accent"
                data-testid="exposure-caps-daily-input"
              />
              <button
                onClick={applyDailyCap}
                disabled={caps.loading || caps.saving}
                className="px-3 py-1 border-2 border-rd-accent text-rd-accent font-mono text-[10px] uppercase tracking-widest font-bold hover:bg-rd-accent hover:text-rd-bg disabled:opacity-40 disabled:cursor-not-allowed"
                data-testid="exposure-caps-daily-apply-button"
                title="Apply this daily cap as the Mongo override (wins over env)"
              >
                {caps.saving ? "Saving…" : "Set cap"}
              </button>
            </div>
          </div>
          {caps.sources?.mongo?.updated_at && (
            <div className="font-mono text-[9px] text-rd-dim">
              last set: {caps.sources.mongo.updated_at}
              {caps.sources.mongo.updated_by ? ` · by ${caps.sources.mongo.updated_by}` : ""}
            </div>
          )}
          {caps.error && (
            <div className="font-mono text-[10px] text-rd-danger flex items-start gap-1">
              <XCircle size={10} weight="bold" className="mt-0.5 shrink-0" />
              {caps.error}
            </div>
          )}
        </div>

        {armState.error && (
          <div className="font-mono text-[10px] text-rd-danger flex items-start gap-1">
            <XCircle size={10} weight="bold" className="mt-0.5 shrink-0" />
            {armState.error}
          </div>
        )}
        {armState.result && (
          <div className="font-mono text-[10px] border-t border-rd-accent/30 pt-1.5 space-y-0.5" data-testid="system-arm-result">
            <div className={armState.result.readiness?.ready_to_trade ? "text-rd-success" : "text-rd-warn"}>
              {armState.result.readiness?.ready_to_trade
                ? "✓ READY — orders will fire when an intent qualifies (subject to market hours)"
                : `⚠ ${armState.result.readiness?.summary || "partial"}`}
            </div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
              {(armState.result.switches || []).map((s) => (
                <div key={s.switch} className="flex items-center gap-1.5">
                  <span style={{ color: s.ok ? "#10B981" : "#DC2626" }}>{s.ok ? "✓" : "✗"}</span>
                  <span className="text-rd-text">{s.switch}</span>
                </div>
              ))}
            </div>
            {(armState.result.readiness?.checks || []).filter((c) => c.status === "red").map((c) => (
              <div key={c.name} className="text-rd-danger pl-2">
                · {c.name}: {c.detail}
                {c.fix_endpoint && (
                  <span className="text-rd-dim block pl-3 text-[9px]">fix → {c.fix_endpoint}</span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {data && (
        <>
          {/* Headline: execution rate */}
          <div className="border border-rd-border bg-rd-bg p-2 grid grid-cols-3 gap-2 text-center" data-testid="post-mortem-headline">
            <div>
              <div className="font-mono text-[9px] uppercase text-rd-dim">Total intents</div>
              <div className="font-mono text-xl text-rd-text">{total}</div>
            </div>
            <div>
              <div className="font-mono text-[9px] uppercase text-rd-dim">Executed</div>
              <div className="font-mono text-xl" style={{ color: executedCount > 0 ? "#10B981" : "#DC2626" }}>
                {executedCount}
              </div>
            </div>
            <div>
              <div className="font-mono text-[9px] uppercase text-rd-dim">Execution rate</div>
              <div className="font-mono text-xl" style={{ color: executePct >= 5 ? "#10B981" : executePct >= 1 ? "#F59E0B" : "#DC2626" }}>
                {executePct.toFixed(1)}%
              </div>
            </div>
          </div>

          {/* ─── Funnel deltas tile (2026-02-20) ─────────────────────
              Post-deploy proof that the doctrine patch landed. Polls
              every 30s. Sits directly under the headline because the
              operator's first question after a deploy is "did this
              actually change anything?" — answer it inline before
              they scroll to the outcome distribution. */}
          <FunnelDeltasTile />

          {/* Biggest funnel drop */}
          {data.biggest_funnel_drop && (
            <div className="border border-rd-warn bg-rd-warn/5 p-2 font-mono text-[11px] text-rd-warn flex items-start gap-1.5" data-testid="post-mortem-funnel-drop">
              <Warning size={11} weight="bold" className="mt-0.5 shrink-0" />
              <span>Biggest funnel drop: {data.biggest_funnel_drop}</span>
            </div>
          )}

          {/* Ghost-intent replay (2026-02-20) — escape hatch when
              the "Never submitted (no audit row)" bucket dominates */}
          {(data.by_outcome?.never_submitted || 0) > 0 && (
            <div className="border border-rd-border bg-rd-bg p-2 space-y-1.5" data-testid="post-mortem-replay-ghosts-block">
              <div className="font-mono text-[10px] text-rd-dim">
                {data.by_outcome.never_submitted} intent{data.by_outcome.never_submitted === 1 ? "" : "s"} have no audit row.
                Replay through the bulletproof chain to surface the actual blocker.
              </div>
              <button
                onClick={replayGhosts}
                disabled={replayState.running}
                className="px-2 py-1 border border-rd-accent text-rd-accent font-mono text-[10px] uppercase tracking-widest hover:bg-rd-accent hover:text-rd-bg disabled:opacity-50"
                data-testid="post-mortem-replay-ghosts-button"
              >
                {replayState.running
                  ? "Replaying…"
                  : `Replay ${Math.min(500, data.by_outcome.never_submitted)} ghost intents`}
              </button>
              {replayState.result && (
                <div className="font-mono text-[10px] text-rd-text border-t border-rd-border pt-1.5" data-testid="post-mortem-replay-result">
                  <div>
                    Scanned <span className="text-rd-accent">{replayState.result.scanned}</span>{" "}
                    · Replayed <span className="text-rd-accent">{replayState.result.replayed}</span>{" "}
                    · Errors <span className={replayState.result.errors ? "text-rd-danger" : "text-rd-dim"}>{replayState.result.errors}</span>
                  </div>
                  <div className="text-rd-dim mt-0.5">
                    {Object.entries(replayState.result.by_terminal_kind || {})
                      .filter(([, n]) => n > 0)
                      .map(([k, n]) => `${k}=${n}`)
                      .join(" · ") || "no terminal rows written (likely scope/window issue)"}
                  </div>
                  {replayState.result.remaining_ghosts_estimate > 0 && (
                    <div className="text-rd-warn mt-0.5">
                      ~{replayState.result.remaining_ghosts_estimate} ghosts remain — click again to drain.
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Outcome distribution */}
          <div>
            <div className="font-mono text-[9px] uppercase text-rd-dim mb-1">Outcome distribution</div>
            <div className="space-y-0.5" data-testid="post-mortem-outcomes">
              {Object.entries(data.by_outcome || {})
                .sort((a, b) => b[1] - a[1])
                .map(([k, n]) => {
                  const meta = prettyLabelFor(k);
                  const pct = total > 0 ? (100 * n / total) : 0;
                  return (
                    <div key={k} className="flex items-center gap-2 font-mono text-[10px]">
                      <div className="w-3 h-3 shrink-0" style={{ background: meta.color }} />
                      <div className="flex-1 text-rd-text">{meta.label}</div>
                      <div className="text-rd-dim w-8 text-right">{n}</div>
                      <div className="text-rd-dim w-12 text-right">{pct.toFixed(1)}%</div>
                    </div>
                  );
                })}
            </div>
          </div>

          {/* Top blockers */}
          {data.top_blockers && data.top_blockers.length > 0 && (
            <div>
              <div className="font-mono text-[9px] uppercase text-rd-dim mb-1">
                Top blockers — fix these and trades unblock
              </div>
              <div className="space-y-0.5" data-testid="post-mortem-blockers">
                {data.top_blockers.map((b, i) => (
                  <div key={`${b.category}-${b.name}`} className="flex items-start gap-2 font-mono text-[10px] border-l-2 border-rd-danger pl-2 py-0.5">
                    <div className="text-rd-dim w-6 shrink-0">#{i + 1}</div>
                    <div className="text-rd-warn shrink-0">[{b.category}]</div>
                    <div className="flex-1 text-rd-text break-all">{b.name}</div>
                    <div className="text-rd-text font-bold w-8 text-right shrink-0">{b.count}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* By lane / by brain — collapsed compact view */}
          <details className="font-mono text-[10px]">
            <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
              Breakdown by lane + brain ▾
            </summary>
            <div className="grid grid-cols-2 gap-2 mt-1">
              <div>
                <div className="text-rd-dim text-[9px] uppercase mb-0.5">By lane</div>
                {Object.entries(data.by_lane || {}).map(([lane, outcomes]) => (
                  <div key={lane} className="border border-rd-border p-1 mb-1">
                    <div className="text-rd-text font-bold uppercase">{lane}</div>
                    {Object.entries(outcomes).map(([k, n]) => (
                      <div key={k} className="flex justify-between text-rd-dim">
                        <span>{k}</span>
                        <span className="text-rd-text">{n}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div>
                <div className="text-rd-dim text-[9px] uppercase mb-0.5">By brain</div>
                {Object.entries(data.by_brain || {}).map(([brain, outcomes]) => (
                  <div key={brain} className="border border-rd-border p-1 mb-1">
                    <div className="text-rd-text font-bold uppercase">{brain}</div>
                    {Object.entries(outcomes).map(([k, n]) => (
                      <div key={k} className="flex justify-between text-rd-dim">
                        <span>{k}</span>
                        <span className="text-rd-text">{n}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          </details>

          {executedCount > 0 && (
            <div className="font-mono text-[10px] text-rd-success flex items-center gap-1.5">
              <CheckCircle size={10} weight="bold" />
              Recent executions: {data.executed_samples.slice(0, 5).map((id) => id.slice(0, 8)).join(", ")}
            </div>
          )}

          {/* ─── Per-intent TRACE block (2026-02-20) ─────────────────
              "Show me a single intent and trace every step until it
              became a broker order or died." Hit GET /admin/intents/
              {intent_id}/trace and render the full timeline + verdict. */}
          <div className="border border-rd-border bg-rd-bg p-2 space-y-1.5" data-testid="intent-trace-block">
            <div className="flex items-center gap-2">
              <MagnifyingGlass size={11} weight="bold" className="text-rd-accent" />
              <span className="font-mono text-[10px] uppercase tracking-widest text-rd-text font-bold">
                Trace one intent
              </span>
              <span className="font-mono text-[9px] text-rd-dim">
                paste any intent_id · see gate-by-gate timeline + where it died
              </span>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={traceState.intentId}
                onChange={(e) => setTraceState((s) => ({ ...s, intentId: e.target.value }))}
                onKeyDown={(e) => e.key === "Enter" && runTrace(traceState.intentId)}
                placeholder="intent_id (e.g. dc4abe17-52f5-46c2-8538-54e4075a3604)"
                className="flex-1 bg-rd-bg2 border border-rd-border px-2 py-1 font-mono text-[10px] text-rd-text placeholder:text-rd-dim focus:outline-none focus:border-rd-accent"
                data-testid="intent-trace-input"
              />
              <button
                onClick={() => runTrace(traceState.intentId)}
                disabled={traceState.loading || !(traceState.intentId || "").trim()}
                className="px-3 py-1 border border-rd-accent text-rd-accent font-mono text-[10px] uppercase tracking-widest hover:bg-rd-accent hover:text-rd-bg disabled:opacity-40"
                data-testid="intent-trace-button"
              >
                {traceState.loading ? "Tracing…" : "Trace"}
              </button>
            </div>

            {/* Recent execution_samples as one-click chips. */}
            {(data.executed_samples || []).length > 0 && (
              <div className="flex items-center gap-1 flex-wrap">
                <span className="font-mono text-[9px] text-rd-dim">recent:</span>
                {data.executed_samples.slice(0, 6).map((id) => (
                  <button
                    key={id}
                    onClick={() => runTrace(id)}
                    className="px-1.5 py-0.5 font-mono text-[9px] border border-rd-border text-rd-dim hover:text-rd-accent hover:border-rd-accent"
                    data-testid={`intent-trace-chip-${id.slice(0, 8)}`}
                  >
                    {id.slice(0, 8)}
                  </button>
                ))}
              </div>
            )}

            {traceState.error && (
              <div className="font-mono text-[10px] text-rd-danger flex items-start gap-1" data-testid="intent-trace-error">
                <XCircle size={10} weight="bold" className="mt-0.5 shrink-0" />
                {traceState.error}
              </div>
            )}

            {traceState.result && (
              <div className="font-mono text-[10px] border-t border-rd-border pt-1.5 space-y-1" data-testid="intent-trace-result">
                <div>
                  <span className="text-rd-dim">VERDICT</span>{" "}
                  <span
                    className="font-bold"
                    style={{
                      color: traceState.result.verdict === "executed"
                        ? "#10B981"
                        : traceState.result.verdict?.startsWith("skipped")
                          ? "#A1A1AA"
                          : "#DC2626",
                    }}
                  >
                    {traceState.result.verdict}
                  </span>
                </div>
                <div className="text-rd-text">{traceState.result.summary}</div>
                {traceState.result.intent && (
                  <div className="text-rd-dim">
                    intent: {traceState.result.intent.stack} {traceState.result.intent.action}{" "}
                    {traceState.result.intent.symbol} conf={traceState.result.intent.confidence}{" "}
                    lane={traceState.result.intent.lane} executed={String(traceState.result.intent.executed)}
                  </div>
                )}
                <div className="text-rd-dim text-[9px] uppercase mt-1">timeline</div>
                <div className="space-y-0.5 max-h-60 overflow-y-auto">
                  {(traceState.result.timeline || []).map((ev, i) => (
                    <div key={i} className="border-l-2 border-rd-border pl-2 py-0.5">
                      <div className="text-rd-text">
                        <span className="text-rd-dim">[{(ev.ts || "").slice(11, 23)}]</span>{" "}
                        {ev.summary}
                      </div>
                      {ev.gate_name && (
                        <div className="text-rd-warn text-[9px] pl-2">
                          → gate <span className="font-bold">{ev.gate_name}</span>:{" "}
                          {(ev.gate_reason || "").slice(0, 240)}
                        </div>
                      )}
                    </div>
                  ))}
                  {!(traceState.result.timeline || []).length && (
                    <div className="text-rd-dim italic">no audit rows — this is a ghost intent</div>
                  )}
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
